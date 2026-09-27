# 事故紀錄

> 這些故障是**我刻意製造的**，目的是在安全的環境裡把排查流程走一遍。
> 每一個都是 LLM 服務在 Kubernetes 上的典型死法。

**為什麼要做這件事**：部署過和排查過是兩回事。一份寫得出排查路徑的事故報告，比任何架構圖都能證明後者。

**寫法**：`排查路徑` 那一段要誠實寫下走錯的岔路。沒有岔路的事故報告看起來像編的。

---

## 事故 1：記憶體上限設太低 → OOMKilled

**日期**：2026-09-27（Phase 0，OrbStack 單節點）
**觸發方式**：`kubectl -n pricer set resources deploy/model-server --limits=memory=2Gi`

> 以下輸出是依本 lab 的設定重建的代表性輸出，不是原始終端紀錄（演練當下沒有留存）。

### 現象
```
$ kubectl -n pricer get pods -l app=model-server -w
NAME                            READY   STATUS             RESTARTS      AGE
model-server-xxxxxxxxxx-xxxxx   0/1     Running            0             15s
model-server-xxxxxxxxxx-xxxxx   0/1     OOMKilled          0             22s
model-server-xxxxxxxxxx-xxxxx   0/1     CrashLoopBackOff   1 (5s ago)    27s
model-server-xxxxxxxxxx-xxxxx   0/1     OOMKilled          2 (20s ago)   50s
```
pod 永遠到不了 `1/1`，`RESTARTS` 一直爬，間隔因 back-off 越拉越長。

### 排查路徑
1. `kubectl logs` → 只看到模型載入到一半的訊息，然後斷掉，沒有任何錯誤。**第一個岔路**：看起來像程式自己卡住，其實行程是被核心直接砍掉的，沒機會寫 log。
2. `kubectl describe pod | grep -A5 "Last State"` →
   ```
   Last State:     Terminated
     Reason:       OOMKilled
     Exit Code:    137
   ```
3. 對照 Prometheus 的實測：model-server 載完 base（Q8_0，3.2 GiB）+ LoRA 後的穩定用量約 4.3 GiB，壓測高峰 4.66 GiB（`--cache-ram 0` 之後）。2Gi 連模型檔本身都裝不下。

### 根因
container 的 memory limit（2Gi）小於模型載入所需的峰值（約 4.3 GiB 以上）。cgroup 記憶體用量一碰到 limit，核心就用 SIGKILL 砍掉行程，kubelet 回報 `OOMKilled`。

### 修法
還原成 local overlay 的值（`k8s/overlays/local/kustomization.yaml`）：
```diff
   resources:
     limits:
-      memory: 2Gi
+      memory: 7Gi
```
或直接 `kubectl apply -k k8s/overlays/local`，讓 Git 裡的 manifest 蓋回去。

### 預防
LLM 服務的記憶體在**載入完成的瞬間**達到峰值，之後才穩定。limit 必須容納那個峰值，不是穩定值。
（→ 這也是為什麼 vLLM 的 `--gpu-memory-utilization` 要預先劃定：它把這件事變成可預測的。）

設 limit 前先量：`max_over_time(container_memory_working_set_bytes{pod=~"model-server.*"}[1h])`（OrbStack 上不要加 `container!=""`，它的 cAdvisor 沒有 container label）。本機實測高峰 4.66 GiB，7Gi 留約 50% 餘裕。

### 學到什麼
- Exit Code **137** = 128 + 9 (SIGKILL) = OOMKilled 的指紋
- **log 裡不會有任何東西**，所以要看 `describe` 的 `Last State` 而不是 `kubectl logs`
- 相關的真實事故：同一天的 HPA 壓測中，llama-server 預設的 prompt 快取（`--cache-ram` 8192 MiB）讓用量從 4.3 漲到 6.5 GiB，把整台 8 GiB VM 擠到 System OOM。那次沒有碰到 container limit，被砍的是沒設 requests 的 grafana（BestEffort）。**container OOMKilled 和 node System OOM 是兩件事**：前者看 `describe`，後者看 `kubectl get events --field-selector reason=SystemOOM`。

---

## 事故 2：startupProbe 容忍時間不夠 → 無限 CrashLoop

**日期**：2026-09-27（Phase 0，OrbStack 單節點）
**觸發方式**：把 `startupProbe.failureThreshold` 從 60 改成 2

> 以下輸出是依本 lab 的設定重建的代表性輸出，不是原始終端紀錄（演練當下沒有留存）。

### 現象
```
$ kubectl -n pricer get pods -l app=model-server -w
NAME                            READY   STATUS             RESTARTS      AGE
model-server-xxxxxxxxxx-xxxxx   0/1     Running            0             18s
model-server-xxxxxxxxxx-xxxxx   0/1     Running            1 (1s ago)    32s
model-server-xxxxxxxxxx-xxxxx   0/1     CrashLoopBackOff   2 (3s ago)    58s
```
狀態在 `Running` 和 `CrashLoopBackOff` 之間來回，但 READY 永遠是 `0/1`。

### 排查路徑
1. `kubectl logs` → 看起來完全正常，就是在載模型。**第一個岔路**：log 沒有任何錯誤，很容易以為「只是還沒載完，再等等」。
2. `kubectl describe pod | grep -i "startup probe"` →
   ```
   Warning  Unhealthy  ...  Startup probe failed: HTTP probe failed with statuscode: 503
   Normal   Killing    ...  Container llama-server failed startup probe, will be restarted
   ```
   llama-server 載模型期間 `/health` 回 503，這是正常行為；問題是 probe 只等 2 次就放棄。
3. `kubectl logs --previous` → 上一個容器正在正常載入，被砍在半路，沒有任何錯誤。

### 根因
容忍時間 = `failureThreshold × periodSeconds` = 2 × 10 = **20 秒**
實測冷啟動時間 = **約 70 秒**（權重已在 PVC；同日 rollout 實測，見 `RESULTS.md`）

每次重啟都要從頭載模型，永遠載不完 20 秒 → 永遠被砍 → 無限迴圈。

### 修法
```diff
   startupProbe:
     httpGet:
       path: /health
       port: http
     periodSeconds: 10
-    failureThreshold: 2
+    failureThreshold: 60
```

### 預防
部署新模型時，**先用 `kubectl logs -f` 手動量一次冷啟動時間**，再把 `failureThreshold` 設成那個值的 2 倍以上。本機 70 秒 → 至少 14 次；設 60（600 秒）是因為 CPU 載入時間不穩定（30–90 秒），留約 7 倍餘裕很便宜。**下載不用算進去**：probe 要等 initContainer 全部跑完、主容器啟動後才開始計時。

### 學到什麼
- `startupProbe` 不是「可選的優化」，對 LLM 服務是必需品
- 這個錯誤在 GPU 上更嚴重：vLLM 還要編 CUDA graph，啟動更慢，而且你是在按小時計費
- **log 正常不代表沒事** —— probe 的失敗只會出現在 events 裡
- startupProbe 失敗重啟的是**容器**，不是 pod：initContainer 不會重跑，所以 `fetch-base-model` 的 log 不會重複出現

---

## 事故 3：initContainer 失敗 → 卡在 Init

**日期**：2026-09-27（Phase 0，OrbStack 單節點）
**觸發方式**：把 LoRA adapter 從 PVC 移走（`breaker` pod 把 `pricer-lora.gguf` 改名成 `.bak`），再刪掉 model-server pod

> 以下輸出是依本 lab 的設定重建的代表性輸出，不是原始終端紀錄（演練當下沒有留存）。

### 現象
```
$ kubectl -n pricer get pods -l app=model-server -w
NAME                            READY   STATUS                  RESTARTS      AGE
model-server-xxxxxxxxxx-xxxxx   0/1     Init:0/1                0             2s
model-server-xxxxxxxxxx-xxxxx   0/1     Init:Error              0             4s
model-server-xxxxxxxxxx-xxxxx   0/1     Init:CrashLoopBackOff   1 (3s ago)    8s
```
主容器 `llama-server` 完全沒有啟動。

### 排查路徑
1. `kubectl logs <pod>` → 看不到任何應用程式 log，只有：
   ```
   Error from server (BadRequest): container "llama-server" in pod "model-server-..." is waiting to start: PodInitializing
   ```
   **第一個岔路**：預設看的是主容器，而主容器根本還沒開始。
2. `kubectl describe pod | grep -A10 "Init Containers"` → `fetch-base-model` 是 `Terminated`，`Reason: Error`，`Exit Code: 1`。
3. `kubectl logs <pod> -c fetch-base-model` →
   ```
   base.gguf 已存在，跳過下載
   錯誤：/models/pricer-lora.gguf 不存在。
   請先照 docs/02-phase0-local-k8s.md 的 Part 1 產生、Part 3 複製進 PVC。
   ```

### 根因
PVC 裡的 `pricer-lora.gguf` 不見了（被改名成 `.bak`）。initContainer 檢查 adapter 不存在就 `exit 1`，initContainer 沒成功，主容器就不會啟動。

### 修法
不是 manifest 的問題，是資料的問題，所以修法是把檔案放回去：
```bash
# fixer pod 掛同一個 PVC，把檔名改回來
mv /models/pricer-lora.gguf.bak /models/pricer-lora.gguf
```
然後 `kubectl -n pricer rollout restart deploy/model-server`。

### 預防
這個 initContainer 是**刻意設計成「找不到 adapter 就失敗」**的。

如果它靜靜地跳過，服務會正常起來、正常回答、但 MAE 是 $110 而不是 $40。
**開不起來的錯誤，比安靜答錯的錯誤好一百倍。**

### 學到什麼
- initContainer 的 log **不會**出現在 `kubectl logs <pod>` 的預設輸出，一定要加 `-c <名字>`
- 「fail fast vs 優雅降級」對推論服務的答案是 **fail fast** —— 因為降級後的錯誤是無聲的
- 錯誤訊息直接寫出「去哪裡修」（Part 1 產生、Part 3 複製），排查時間從分鐘級變秒級

---

## 事故 4（Phase 1）：`--max-lora-rank` 的無聲截斷

**這一個跟前三個不同 —— 它不會讓任何東西壞掉。**

**觸發方式**：啟動 vLLM 時不帶 `--max-lora-rank`（用預設值 16），但 adapter 是 r=256

### 現象
| 檢查項目 | 結果 |
|---|---|
| Pod 狀態 | ✅ Running |
| startupProbe | ✅ 通過 |
| readinessProbe | ✅ 通過 |
| `/v1/models` | ✅ 列出 adapter |
| API 回應 | ✅ 200，回傳合法的價格數字 |
| **MAE** | ❌ 從 $___ 掉到 $___ |

### 排查路徑
1. 
2. 
3. 

### 根因
vLLM 的 `--max-lora-rank` 支援值是 8/16/32/64/128/256/320/512，**預設 16**。
adapter 的 rank 超過設定值時，vLLM 會**載入它能載的部分**，而不是拒絕啟動。

### 修法
```diff
```

### 預防
**在部署流程裡加一道業務層面的正確性關卡**：

```
部署 → health check 通過 → 跑 50 筆的 MAE smoke test → MAE < 門檻才放流量
```

在 k8s 裡就是 rollout 之後跑一個評估 Job，失敗就 `kubectl rollout undo`。

### 學到什麼
**這是整個 lab 最重要的一課：服務起來 ≠ 正確。**

所有的 liveness、readiness、`/v1/models`、HTTP 200 —— 全部都是綠的。
**只有業務層面的驗證抓得到這種錯誤。**

這件事也解釋了為什麼這個 lab 堅持「每個實驗都同時量正確性和效能」。
