# LLM Inference on Kubernetes 實驗室 — 90：排錯速查

> 這一份是**查表用的**，不是讀物。遇到症狀就按 Ctrl+F。
> 深入的事故分析在 [`POSTMORTEMS.md`](../POSTMORTEMS.md)。

---

## 先問這三個問題

排查 GPU + k8s 問題時，**永遠由下往上**。搞錯順序會浪費很多時間：

```
1. nvidia-smi                        驅動層 —— 主機看得到卡嗎
2. docker run --gpus all … nvidia-smi   runtime 層 —— 容器看得到嗎
3. kubectl describe node | grep nvidia  排程層 —— k8s 知道有卡嗎
4. kubectl exec … nvidia-smi            pod 層 —— pod 真的拿到了嗎
```

**在第 N 層失敗，就不要去查第 N+1 層。**

---

## A. 正確性問題（最危險的一類）

這類問題的共同特徵是：**服務完全正常、健康檢查全綠、API 回應合法 —— 但答案是錯的。**

| 症狀 | 最可能的原因 | 確認方式 | 修法 |
|---|---|---|---|
| **MAE 約 $110** | LoRA 完全沒生效 —— 請求打到 base 了 | 打 `--model base` 比對，如果兩者一樣就確定了 | 檢查請求的 `model` 欄位（pricer-api 的 `MODEL_NAME`）是不是 adapter 名稱 |
| **MAE 略差但不離譜** | 量化漂移 | 換 bf16 跑一次對照 | 這是預期行為，記錄下來不是修 |
| 回傳的價格總是 0 | prompt 模板不對，或 parse 失敗 | 看 `raw` 欄位的原始文字 | `Price is $` 的 `$` 後面**不能有空白** |
| `IndexError` | 照抄了 Modal 版的 `split("Price is $")[1]` | — | vLLM / llama.cpp 只回續寫部分，不含 prompt。直接對回傳做 regex |
| 回答變成一整段文字 | 用了 `/v1/chat/completions` | — | base model 沒有 chat template，**必須用 `/v1/completions`** |

**🔑 這一類問題的通用教訓**：health check 抓不到正確性問題。每個推論服務的部署流程都該有一道**業務層面的驗證關卡**（例如 rollout 後跑 50 筆的 MAE smoke test）。

---

## B. Pod 起不來

| 症狀 | 怎麼確認 | 原因與修法 |
|---|---|---|
| `CrashLoopBackOff` + `Exit Code: 137` | `kubectl describe pod \| grep -A5 "Last State"` | **OOMKilled**。log 裡什麼都不會有（被核心直接砍）。調大 memory limit。注意 LLM 的記憶體在**載入完成的瞬間**達到峰值 |
| `CrashLoopBackOff` 但 log 看起來正常 | `kubectl describe pod \| grep -i "startup probe"` | **`startupProbe` 時間不夠**。容忍時間 = `failureThreshold × periodSeconds`，要 > 冷啟動時間的 2 倍 |
| `Init:Error` / `Init:CrashLoopBackOff` | `kubectl logs <pod> **-c <initContainer名>**` | initContainer 失敗。⚠️ 不加 `-c` 看不到它的 log，這是最常卡住人的地方 |
| `ImagePullBackOff`（本機 build 的 image） | `kubectl describe pod` | `imagePullPolicy` 要設 `IfNotPresent`。⚠️ **tag 是 `:latest` 時 k8s 強制用 `Always`**，改用 `:dev` |
| `ErrImageNeverPull`（kind） | — | 忘了 `kind load docker-image <name> --name <cluster>` |
| `Pending` + `Insufficient nvidia.com/gpu` | `kubectl describe pod \| grep -A5 FailedScheduling` | 卡不夠。單卡做滾動更新必然如此 —— 見 Phase 3 Part 6 的三個選項 |
| `Pending` + `untolerated taint` | 同上 | GPU 節點有 taint，pod 缺 toleration。**記得 toleration 和 nodeSelector 要成對設** |
| `Pending` + `didn't match node affinity` | 同上 | nodeSelector 的標籤在這個叢集不存在。EKS 上沒有 `nvidia.com/gpu.present`（那是 GPU Operator 的 NFD 打的） |
| `ContainerCreating` + `Multi-Attach error` | `kubectl describe pod \| grep -i multi-attach` | **RWO 的 PVC 已經被別的節點掛走了**。單節點上不會發生，多節點會。改用 RWX 或讓 pod 排在同一節點 |
| `Pending` 且 PVC 也 `Pending` | `kubectl describe pvc` | StorageClass 是 `WaitForFirstConsumer`，要有 pod 要用才配置。這通常是對的行為 |

---

## C. vLLM 專屬

| 症狀 | 原因 | 修法 |
|---|---|---|
| 負載一上來就 crash、shared memory 相關錯誤 | `/dev/shm` 預設只有 64MB | Docker：`--ipc=host`；**k8s：掛 `emptyDir: {medium: Memory}` 到 `/dev/shm`** |
| 掛了 `/dev/shm` 之後改成 OOMKilled | `medium: Memory` 的 emptyDir **會算進容器的記憶體 limit** | memory limit 要加上 shm 的大小 |
| 啟動時 CUDA OOM，或報可用顯存不足 | `--gpu-memory-utilization` 太高，或同卡上有別的 pod | time-slicing 時每個 pod 的值要**除以 pod 數**，而且每一份都要放得下「權重 + LoRA + CUDA 開銷」—— bf16 的 3B 模型在 L4 上最多 2 個 pod（見 docs/06 Part 7.2） |
| 403 / gated repo | 沒有 HF token，或沒接受授權 | 確認 Secret 掛對了，且已在 HF 網頁接受 Llama 3.2 授權 |
| 啟動極慢（>5 分鐘） | 在下載權重 + 編 CUDA graph | 把 HF cache 放 PVC；debug 時可暫時用 `--enforce-eager` 跳過 CUDA graph |
| 啟動就失敗：`... KV cache is needed, which is larger than the available KV cache memory` | `--max-model-len` 太大（預設 131072），一條最長序列的 KV cache 放不下 | 調小 `--max-model-len`（本 lab 用 512），或調高 `--gpu-memory-utilization`。錯誤訊息裡的 `estimated maximum model length` 就是這張卡撐得住的上限 |

---

## D. 可觀測性

| 症狀 | 原因 | 修法 |
|---|---|---|
| Prometheus 的 `/targets` 沒有你的服務 | **ServiceMonitor 的 `release` label 不對**（第一名的原因） | `kubectl get prometheus -A -o jsonpath='{.items[0].spec.serviceMonitorSelector}'` 查實際要求的 label |
| ServiceMonitor 存在但沒抓到 | `endpoints.port` 填了**號碼**而不是 Service 裡 port 的**名字** | 改成名字，並確認 Service 的 port 有命名 |
| ServiceMonitor 的 selector 選不到 | selector 比對的是 **Service** 的 label，不是 Deployment 的 | 檢查 `kubectl get svc --show-labels` |
| HPA 的 `TARGETS` 顯示 `<unknown>` | prometheus-adapter 沒正確暴露指標 | 依序查：① `kubectl get --raw /apis/custom.metrics.k8s.io/v1beta1` ② Prometheus 裡那個 series 有沒有資料 ③ `kubectl logs deploy/prometheus-adapter` |
| HPA 一直震盪擴縮 | 沒設 `behavior.scaleUp.stabilizationWindowSeconds` | 設成 > 冷啟動時間（本 lab 是 180 秒） |
| CPU HPA 永遠不觸發 | **這是正確行為，不是 bug** | LLM 的瓶頸不是 CPU。改用 `num_requests_waiting` 這類佇列指標 |
| `/metrics` 是 404 或空的 | llama.cpp 的 metrics **預設是關的** | 加 `--metrics` 旗標 |

---

## E. 網路與流量

| 症狀 | 原因 | 修法 |
|---|---|---|
| 串流變成「卡很久然後一次跳出來」 | nginx 的 `proxy_buffering` 預設開著 | Ingress 加 `nginx.ingress.kubernetes.io/proxy-buffering: "off"` |
| 長請求在 60 秒被切斷 | `proxy-read-timeout` 預設 60 秒 | 加 `proxy-read-timeout: "300"` |
| 刪 pod 時有零星請求失敗 | endpoint 移除與 SIGTERM 是**平行**的 | 加 `preStop: sleep 10`，並確認 `terminationGracePeriodSeconds` 夠大 |
| 後端重啟時整條服務炸掉 | client 沒有 timeout / retry | 見 `src/pricer-api/app.py`，`temperature=0` 讓重試是安全的 |
| NetworkPolicy apply 成功但完全沒效果 | **CNI 不執行 NetworkPolicy** | kindnet 和部分 CNI 都不執行。換 Calico。⚠️ **測不出效果就不要宣稱會這個** |
| LoadBalancer 的 EXTERNAL-IP 永遠 `<pending>` | 沒有雲端供應商控制器 | on-prem 要裝 MetalLB，且 IP 池必須落在實際網段內 |
| Ingress annotation 完全沒作用 | **annotation 是廠商特定的** | nginx 的 annotation 在 Traefik 上無效。k3s 預設裝 Traefik |

---

## F. 設定沒生效

| 症狀 | 原因 | 修法 |
|---|---|---|
| 改了 ConfigMap 但服務行為沒變 | **k8s 不會因為 ConfigMap 變更而重啟 pod** | 用 Kustomize 的 `configMapGenerator`（保留雜湊後綴），設定一改就自動觸發 rollout |
| `kubectl kustomize` 報 `security; file is not in or below` | Kustomize 的 load restrictor 不准讀取 kustomization 目錄以外的檔案 | 把檔案搬進 base 目錄。**不要**用 `--load-restrictor LoadRestrictionsNone`，那是會傳染的壞習慣 |
| Helm 升級後行為怪異 | **`helm upgrade` 不會更新 CRD** | 手動 apply 新版 CRD |
| 不確定 Helm 實際生效的值 | `--set` 的優先序高於 `-f values.yaml` | `helm get values <release> -n <ns>` |
| `helm uninstall` 之後還有殘留 | uninstall 不刪 CRD 也不刪 PVC | 手動檢查並清理 |

---

## G. 本機環境（Mac / OrbStack / kind）

| 症狀 | 原因 | 修法 |
|---|---|---|
| 機器變得很慢、開始 swap | 同時開了 OrbStack k8s 和 kind | **16GB 的機器一次只開一個**。`orb stop k8s` |
| 壓測數字每次都不一樣 | MacBook Air 是被動散熱，會降頻 | **本機不產生效能數字**，只驗證正確性與 k8s 行為 |
| `kubectl cp` 失敗 | 目標容器裡沒有 `tar` | 用 `busybox` 之類有 `tar` 的 image 當搬運工 |
| llama.cpp 的估價變差但沒有錯誤 | `--ctx-size` 是**所有並行槽位共用的總量**，被 `--parallel` 均分 | `--ctx-size` 要 ≥ 單槽需求 × `--parallel` |
| 轉 LoRA 到 GGUF 時記憶體爆掉 | lazy evaluation 有問題 | 加 `--no-lazy` |

---

## 通用的排查指令

```bash
# 事件（按時間排序）—— 通常第一個該看的地方
kubectl -n <ns> get events --sort-by=.lastTimestamp | tail -20

# 上一次 crash 的 log
kubectl -n <ns> logs <pod> --previous

# initContainer 的 log（⚠️ 一定要加 -c）
kubectl -n <ns> logs <pod> -c <initContainer名>

# 為什麼排不進去
kubectl -n <ns> describe pod <pod> | grep -A10 Events

# 節點上實際的資源分配
kubectl describe node <node> | grep -A15 "Allocated resources"

# 比對你送出的 YAML 與實際生效的（會看到 admission webhook 加了什麼）
kubectl -n <ns> get pod <pod> -o yaml | head -60
```
