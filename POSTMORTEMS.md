# 事故紀錄

> 這些故障是**我刻意製造的**，目的是在安全的環境裡把排查流程走一遍。
> 每一個都是 LLM 服務在 Kubernetes 上的典型死法。

**為什麼要做這件事**：部署過和排查過是兩回事。一份寫得出排查路徑的事故報告，比任何架構圖都能證明後者。

**寫法**：`排查路徑` 那一段要誠實寫下走錯的岔路。沒有岔路的事故報告看起來像編的。

---

## 事故 1：記憶體上限設太低 → OOMKilled

**觸發方式**：`kubectl -n pricer set resources deploy/model-server --limits=memory=2Gi`

### 現象
```
（貼 kubectl get pods 的實際輸出，要看得到 RESTARTS 在爬）
```

### 排查路徑
1. `kubectl logs` → （寫下你看到什麼。提示：會是空的或不完整的，因為行程是被核心直接砍的）
2. `kubectl describe pod | grep -A5 "Last State"` → 
3. 

### 根因


### 修法
```diff
```

### 預防
LLM 服務的記憶體在**載入完成的瞬間**達到峰值，之後才穩定。limit 必須容納那個峰值，不是穩定值。
（→ 這也是為什麼 vLLM 的 `--gpu-memory-utilization` 要預先劃定：它把這件事變成可預測的。）

### 學到什麼
- Exit Code **137** = 128 + 9 (SIGKILL) = OOMKilled 的指紋
- **log 裡不會有任何東西**，所以要看 `describe` 的 `Last State` 而不是 `kubectl logs`

---

## 事故 2：startupProbe 容忍時間不夠 → 無限 CrashLoop

**觸發方式**：把 `startupProbe.failureThreshold` 從 60 改成 2

### 現象
```
（貼輸出。注意：這是最陰險的一個 —— log 完全正常，它只是還在載模型）
```

### 排查路徑
1. `kubectl logs` → （看起來完全正常，這是第一個岔路）
2. `kubectl describe pod | grep -i "startup probe"` → 
3. `kubectl logs --previous` → 

### 根因
容忍時間 = `failureThreshold × periodSeconds` = ___ 秒
實測冷啟動時間 = ___ 秒

### 修法
```diff
```

### 預防
部署新模型時，**先用 `kubectl logs -f` 手動量一次冷啟動時間**，再把 `failureThreshold` 設成那個值的 2 倍以上。

### 學到什麼
- `startupProbe` 不是「可選的優化」，對 LLM 服務是必需品
- 這個錯誤在 GPU 上更嚴重：vLLM 還要編 CUDA graph，啟動更慢，而且你是在按小時計費
- **log 正常不代表沒事** —— probe 的失敗只會出現在 events 裡

---

## 事故 3：initContainer 失敗 → 卡在 Init

**觸發方式**：把 LoRA adapter 從 PVC 移走

### 現象
```
（貼輸出。狀態應該是 Init:Error 或 Init:CrashLoopBackOff，主容器完全沒啟動）
```

### 排查路徑
1. `kubectl logs <pod>` → （看不到任何東西，這是第一個岔路）
2. `kubectl describe pod | grep -A10 "Init Containers"` → 
3. `kubectl logs <pod> -c fetch-base-model` → 

### 根因


### 修法
```diff
```

### 預防
這個 initContainer 是**刻意設計成「找不到 adapter 就失敗」**的。

如果它靜靜地跳過，服務會正常起來、正常回答、但 MAE 是 $110 而不是 $40。
**開不起來的錯誤，比安靜答錯的錯誤好一百倍。**

### 學到什麼
- initContainer 的 log **不會**出現在 `kubectl logs <pod>` 的預設輸出，一定要加 `-c <名字>`
- 「fail fast vs 優雅降級」對推論服務的答案是 **fail fast** —— 因為降級後的錯誤是無聲的

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
