# LLM Inference on Kubernetes 實驗室 — 02：Phase 0，本機 k8s 骨架與可觀測性

> 成本：**$0**。全程在你的 Mac 上，不需要 AWS 配額核准。
> 產出：一個能用的 k8s 服務、一整套可觀測性、以及**第一個 MAE 數字**。
> 面試價值：這一階段建立的 manifest，Phase 3 接上真 GPU 時只換一個 overlay。

---

## 這一階段的目的

**把 k8s 這一層的問題全部在零成本環境解決掉。**

理由很現實：ServiceMonitor 的 label 對不上、prometheus-adapter 的 `seriesQuery` 寫錯、HPA 抓不到指標 —— 這些都是純 k8s 的問題，跟 GPU 完全無關，但每一個都要花時間試錯。在按小時計費的 GPU 機器上 debug 這些，是這整個計畫最容易浪費錢的地方。

所以順序是：**先在本機把 k8s 練熟 → 上 GPU 只解決 GPU 的問題。**

```
Phase 0（這裡，$0）        Phase 3（GPU）
┌──────────────────┐      ┌──────────────────┐
│ k8s 骨架         │      │ k8s 骨架  ← 不變 │
│ 可觀測性         │ ───► │ 可觀測性  ← 不變 │
│ HPA / probe / …  │      │ HPA / …   ← 不變 │
│ llama.cpp (CPU)  │      │ vLLM (GPU)← 換這 │
└──────────────────┘      └──────────────────┘
```

**十二個部分：**

1. 把真模型在本機跑起來 —— 拿到第一個 MAE
2. OrbStack k8s 起手式
3. 把模型送進叢集 —— on-prem 的第一個真問題
4. 三種 probe 的分工
5. 優雅關機
6. pricer-api 與服務間呼叫
7. Ingress 與 streaming 的坑
8. 可觀測性
9. HPA：CPU 為什麼是錯的
10. Kustomize：讓這一切能搬到 GPU 上
11. 滾動更新
12. 三個刻意故障演練

---

## Part 1：把真模型在本機跑起來

**目的**：在花任何錢之前，先拿到一個**正確性基準**。之後 GPU 上的每個數字都要跟它比。

### 1.1 做法

```bash
cd ~/Desktop/side-projects/llm-inference-k8s-lab
while IFS='=' read -r k v; do export "$k=$v"; done < <(grep -E '^[A-Z_]+=' k8s/base/model.conf)
mkdir -p models
```

**第一步：抓 base model 的 GGUF**（約 3.4GB，公開 repo，不需授權）

```bash
curl -fL --progress-bar \
  -o models/base.gguf \
  "https://huggingface.co/QuantFactory/Llama-3.2-3B-GGUF/resolve/main/Llama-3.2-3B.Q8_0.gguf"
```

**第二步：抓 LoRA adapter**（r=256，safetensors 約 1.5GB，gated repo，需要憑證）

```bash
uv run --with huggingface_hub python3 - <<'PY'
import os
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id=os.getenv("ADAPTER_REPO"),
    revision=os.getenv("ADAPTER_REVISION"),
    local_dir="models/adapter",
    token=open("private/hf-token").read().strip(),
)
print("下載完成")
PY
```

**第三步：把 adapter 轉成 GGUF**

```bash
git clone --depth 1 https://github.com/ggml-org/llama.cpp /tmp/llama.cpp

LAB=~/Desktop/side-projects/llm-inference-k8s-lab
(cd /tmp/llama.cpp && \
 HF_TOKEN="$(cat $LAB/private/hf-token)" \
 uv run --with torch --with transformers --with safetensors --with numpy \
   python convert_lora_to_gguf.py \
     --base-model-id meta-llama/Llama-3.2-3B \
     --outtype f16 \
     --outfile $LAB/models/pricer-lora.gguf \
     $LAB/models/adapter)
```

**第四步：起服務**

```bash
brew install llama.cpp      # 省得自己編譯

llama-server \
  --model models/base.gguf \
  --lora models/pricer-lora.gguf \
  --ctx-size 2048 \
  --parallel 4 \
  --metrics \
  --port 8080
```

**第五步：拿到第一個 MAE**

```bash
# 另開一個終端
uv run --with datasets eval/measure.py \
  --base-url http://localhost:8080 \
  --model pricer \
  --size 200 \
  --label "phase0-mac-llamacpp-q8-lora-f16"
```

### 1.2 為什麼這樣做

**為什麼用 `--lora` 而不是先 merge**
合併 LoRA 要把 base model 以 fp16 整個載進記憶體（約 6.4GB），加上 adapter 和輸出緩衝，峰值約 8GB —— 在 16GB 的機器上會很痛，還要 15GB 暫存磁碟。而 `convert_lora_to_gguf.py` 的 `--base-model-id` **只從 HF 抓 config，不抓權重**，轉檔過程只碰 adapter 本身。差別是一杯咖啡 vs 一個下午。

**為什麼 base 選 Q8_0 而不是 Q4_K_M**
Q8_0 幾乎無損。這一步的目的是建立**正確性基準**，要把「量化造成的誤差」這個變因壓到最小。如果用 Q4 拿到一個偏掉的 MAE，你會分不清是量化的鍋還是 LoRA 沒生效。等基準確立之後，再拿 Q4 來量「量化的代價」—— 那時候它是被觀測的變因，而不是雜訊。

**為什麼 `--ctx-size 2048` 配 `--parallel 4`**
⚠️ llama.cpp 的 `--ctx-size` 是**所有並行槽位共用的總量**，會被 `--parallel` 均分。4 個槽位、每個需要約 512 → 總共要 2048。設成 512 配 4 個槽位的話，每個槽位只剩 128，長一點的 prompt 會被**無聲截斷**，症狀是 MAE 莫名其妙變差而且沒有任何錯誤訊息。

**為什麼要 `--metrics`**
llama.cpp 的 Prometheus 端點預設是**關的**。這是選它而不是 Ollama 當本機替身的關鍵理由 —— llama.cpp 的指標語意（處理中 / 排隊中的請求數）跟 vLLM 對得上，Part 8 的整套可觀測性（ServiceMonitor、custom metrics HPA）可以在本機先做完。Ollama 長期沒有原生 `/metrics`（2026 年才有 PR 在補），指標名稱也跟 vLLM 不同；用它的話這部分就得延到 GPU 階段才能做，而那是最花時間試錯的部分。

### 1.3 驗證

先手動打一發：

```bash
curl -s http://localhost:8080/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "prompt": "What does this cost to the nearest dollar?\n\nTitle: Bosch 12V Max Cordless Drill\nCategory: Tools\nBrand: Bosch\nDescription: A compact cordless drill for home use.\nDetails: Includes two batteries and a charger.\n\nPrice is $",
    "max_tokens": 5,
    "temperature": 0
  }' | python3 -c "import json,sys; print(repr(json.load(sys.stdin)['choices'][0]['text']))"
```

應該看到類似 `'129.00'` 的東西。

然後看 MAE：

| 你看到的 MAE | 代表什麼 |
|---|---|
| **$35 ~ $50** | ✅ 正常。LoRA 生效了 |
| **$100 ~ $120** | ❌ LoRA **沒有生效**。服務起來了但在用未微調的 base model |
| **$60 ~ $70** | ⚠️ 可能是量化造成的退化，也可能 adapter 只載了一部分。記下來，Phase 1 用 bf16 重跑就能分辨 |

把數字填進 `RESULTS.md`。**這是整個 lab 的第一個數據點。**

### ⚠️ 陷阱

- **MAE 遠高於 $40（$110～$200 這一帶）= LoRA 沒生效。** leaderboard 上「未微調的 Llama 3.2」是 $110.72（課程 evaluator）；用本 repo 的 `eval/measure.py` 量 base，Phase 1 在 vLLM bf16 上實測是 $200.29（llama.cpp 上沒量過）。檢查 `--lora` 路徑對不對、llama-server 啟動 log 有沒有印出載入 adapter 的訊息。
- **這個 adapter 是用 QLoRA（4-bit NF4 base）訓練的**，你現在把它套在 Q8_0 的 base 上。兩者的量化方式不同，**預期會有一些漂移**。這不是 bug，是要被記錄的變因 —— Phase 1 用 bf16 跑一次就能量出漂移有多大。
- **轉檔時如果 lazy evaluation 出錯**，加 `--no-lazy`（會多吃記憶體但比較穩）。
- **`llama-server` 啟動後第一個請求特別慢**，那是 warmup。量測時第一次的結果丟掉。
- **不要對 Air 做長時間壓測**。被動散熱會降頻，數字既不準也不可重現。本機只驗證正確性。

---

## Part 2：OrbStack k8s 起手式

### 2.1 做法

在 OrbStack 設定裡開啟 Kubernetes，然後：

```bash
kubectl config use-context orbstack
kubectl get nodes
kubectl get storageclass
```

### 2.2 為什麼是 OrbStack

| | OrbStack | Docker Desktop | minikube / kind |
|---|---|---|---|
| 記憶體開銷 | 小 | 大 | 中 |
| LoadBalancer | **開箱可用** | 可用 | 卡在 Pending，要裝 MetalLB |
| 對外網域 | `*.k8s.orb.local` 萬用字元 DNS，不用改 hosts | localhost | 要自己搞 |
| 多節點 | ❌ 單節點 | ❌ | ✅ |

**在 16GB 的 Mac 上，OrbStack 是 Phase 0 的最佳選擇。** 它做不到的事（多節點）由 Phase 0.5 的 kind 補。

### ⚠️ 陷阱 —— OrbStack 的便利會給你三個錯誤直覺

這一節很重要，**單節點環境會讓你學到錯的東西**：

1. **`ReadWriteOnce` 的 PVC 在單節點上可以被多個 pod 同時掛載**，而且完全正常運作。因為所有 pod 都在同一個節點上。到真實叢集會撞 `Multi-Attach error`。Phase 0.5 會刻意重現這個失敗。
2. **LoadBalancer 直接就能用**。真實的 on-prem 沒有這種東西，要裝 MetalLB 並處理 IP 池、`externalTrafficPolicy`、ARP/BGP。Phase 0.5 補。
3. **`NetworkPolicy` 不一定有效。** OrbStack 的 CNI 未必會強制執行。**這是最危險的一種誤導** —— 你寫了 YAML、`kubectl apply` 成功、什麼事都沒發生，然後以為自己學會了。

   **開工前先驗證**：
   ```bash
   kubectl create ns nptest
   kubectl -n nptest run probe --image=curlimages/curl:8.11.1 --restart=Never -- sleep 3600
   kubectl -n nptest apply -f - <<'EOF'
   apiVersion: networking.k8s.io/v1
   kind: NetworkPolicy
   metadata: { name: deny-all-egress }
   spec:
     podSelector: {}
     policyTypes: ["Egress"]
   EOF
   kubectl -n nptest exec probe -- curl -s -m 5 https://example.com -o /dev/null -w '%{http_code}\n'
   ```
   **連得出去 = 你的 CNI 沒有強制執行 NetworkPolicy。** 那就把 NetworkPolicy 的練習留到 Phase 0.5（kind + Calico）。
   ```bash
   kubectl delete ns nptest
   ```

---

## Part 3：把模型送進叢集 —— on-prem 的第一個真問題

**目的**：面對「模型權重怎麼進到叢集裡」這個問題。這是 on-prem AI 面試幾乎必問的題目。

### 3.1 做法

先建 namespace 與 PVC，再把本機轉好的 LoRA GGUF 複製進去：

```bash
kubectl apply -f k8s/base/namespace.yaml

# 只建 PVC（Deployment 的 initContainer 會因為找不到 adapter 而失敗，這是預期的）
kubectl -n pricer apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: model-weights }
spec:
  accessModes: ["ReadWriteOnce"]
  resources: { requests: { storage: 20Gi } }
EOF

# 開一個臨時 pod 掛上 PVC，當作「搬運工」
kubectl -n pricer apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata: { name: loader }
spec:
  containers:
    - name: shell
      image: busybox:1.37
      command: ["sleep", "3600"]
      volumeMounts: [{ name: models, mountPath: /models }]
  volumes:
    - name: models
      persistentVolumeClaim: { claimName: model-weights }
EOF

kubectl -n pricer wait --for=condition=Ready pod/loader --timeout=120s
kubectl -n pricer cp models/pricer-lora.gguf loader:/models/pricer-lora.gguf
kubectl -n pricer exec loader -- ls -lh /models
kubectl -n pricer delete pod loader
```

### 3.2 為什麼這樣做（以及真實世界怎麼做）

我們刻意把兩種檔案用兩種方式處理，因為它們的性質不同：

| 檔案 | 怎麼進叢集 | 為什麼 |
|---|---|---|
| **base model**（3.4GB，公開） | initContainer 從 HTTP 下載 | 公開且不變，任何節點都能自己抓 |
| **LoRA adapter**（你產出的） | 手動 `kubectl cp` 到 PVC | 它是你的產物，不在任何公開 registry 上 |

**「模型權重怎麼分發」是 on-prem 的核心難題。** 想像 200GB 的模型、5 個 GPU 節點。四種做法的光譜：

| 做法 | 優點 | 缺點 | 適用 |
|---|---|---|---|
| **烤進 container image** | 版本不可變、air-gap 友善、沒有執行期依賴 | image 變成數百 GB，每個節點 pull 一次很痛 | 模型小、更新極少 |
| **RWX 共享（NFS / Ceph）** | 只存一份、所有節點共用 | 載入慢（走網路）、NFS 變成單點故障與頻寬瓶頸 | 節點多、模型大、啟動時間不敏感 |
| **每節點 local PV + 預先 rsync** | 載入最快（本地 NVMe） | 要自己管一致性與空間回收 | 追求最低冷啟動延遲 |
| **initContainer 從內部物件儲存拉到 local PV** | 折衷，有版本控制 | 每次冷啟動都要拉一次 | **最常見的生產做法** |

我們在 Phase 0 用的是最簡化版（單節點，PVC 就在本地）。**Phase 0.5 會在多節點上實際撞到這些取捨。**

### ⚠️ 陷阱

- **`kubectl cp` 需要容器裡有 `tar`。** `busybox` 有，很多 distroless 或 `scratch` 的 image 沒有，會失敗且錯誤訊息很不直覺。
- **PVC 在沒有 pod 使用時可能不會真的被配置**。如果 StorageClass 是 `WaitForFirstConsumer` 模式，PVC 會一直 `Pending` 直到有 pod 要用它 —— 這就是為什麼上面要開一個 `loader` pod。這個行為在 Phase 0.5 會變成一個更嚴重的問題（PV 被綁到沒有 GPU 的節點上）。
- **`kubectl cp` 大檔案很慢而且沒有進度條**。幾百 MB 可以忍，真實的 200GB 模型絕對不能用這個方式 —— 那正是上表存在的理由。

---

## Part 4：三種 probe 的分工

**目的**：這是 LLM 服務在 k8s 上最常做錯的地方，也是面試高頻題。

### 4.1 做法

看 `k8s/base/model-server.yaml` 裡的三個 probe：

```yaml
startupProbe:                    # 「它還在開機嗎」
  httpGet: { path: /health, port: http }
  periodSeconds: 10
  failureThreshold: 60           # 容忍 600 秒

readinessProbe:                  # 「可以送流量進來嗎」
  httpGet: { path: /health, port: http }
  periodSeconds: 5
  failureThreshold: 3

livenessProbe:                   # 「它是不是死當了」
  httpGet: { path: /health, port: http }
  periodSeconds: 30
  failureThreshold: 5
```

部署並觀察。overlay 裡也包含 pricer-api（Part 6 才細講），它的 image 要先在本機 build，否則會卡在 `ImagePullBackOff`：

```bash
docker build -t pricer-api:dev src/pricer-api
kubectl apply -k k8s/overlays/local
kubectl -n pricer get pods -w
```

model-server 第一次啟動會在 initContainer 下載 base model（約 3.4GB，視網速 10–20 分鐘）。curl 的進度條用 `\r` 覆寫同一行，`kubectl logs` / k9s 只會顯示一行 `0` 看起來像卡住，要這樣看：

```bash
kubectl -n pricer logs deploy/model-server -c fetch-base-model | tr '\r' '\n' | tail -1
```

### 4.2 為什麼這樣做

| Probe | 失敗的後果 | 對 LLM 服務的意義 |
|---|---|---|
| **startupProbe** | 超過容忍時間才殺 pod | **沒有它，LLM 服務會無限重啟。** 在它通過之前，另外兩個 probe 都不會執行 |
| **readinessProbe** | 從 Service 的 endpoint 移除，**不重啟** | 模型還在載、或暫時過載時，把流量導去別的 pod |
| **livenessProbe** | **重啟容器** | 只該抓「行程還在但已經死當」。它一失敗的代價是再等好幾分鐘的載入 |

**核心洞見：一般 web service 啟動只要幾百毫秒，三個 probe 的設定差異不大。LLM 服務啟動要 1–3 分鐘，這個差異就變成生死問題。**

`failureThreshold × periodSeconds` 就是你給它的開機時間預算：

```
60 × 10s = 600 秒
```

本機 CPU 載 3B 大約 30–90 秒（實測約 70 秒），時間不穩定，所以抓寬。**下載 base model 的時間不用算進去**：它發生在 initContainer，probe 要等所有 initContainer 跑完、主容器啟動後才開始計時。Phase 3 用 vLLM 時要重新估（vLLM 還要編 CUDA graph）。

### 4.3 驗證

```bash
# 看 startupProbe 撐住的過程：pod 是 Running 但 READY 是 0/1
kubectl -n pricer get pod -l app=model-server -w

# 確認 restartCount 保持 0 —— 如果它在爬，就是 startupProbe 時間不夠
kubectl -n pricer get pod -l app=model-server \
  -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}{"\n"}'
```

成功條件：pod 最終變成 `1/1 Running`，且 `restartCount` 全程是 `0`。

### ⚠️ 陷阱

- **最經典的錯誤**：不設 `startupProbe`，只用 `livenessProbe` 加一個大的 `initialDelaySeconds`。問題是 `initialDelaySeconds` 是**固定**的猜測值 —— 猜太短就無限重啟，猜太長則真的死當時要等很久才被發現。`startupProbe` 是「給它時間但一好就立刻切換到嚴格模式」，兩全其美。
- **`livenessProbe` 不要去檢查下游依賴**。如果 model-server 的 liveness 去戳資料庫，資料庫一抖就會害所有 pod 被重啟 —— 把單一故障放大成連鎖故障。看 `src/pricer-api/app.py`：`/healthz`（liveness 用）刻意不戳後端，`/readyz`（readiness 用）才戳。
- **probe 的 timeout 預設只有 1 秒**。負載高時服務回應變慢，可能被誤判為死掉。這個 lab 的 `/health` 很輕所以沒設，但如果你的健康檢查會做實際工作，記得調 `timeoutSeconds`。

---

## Part 5：優雅關機

**目的**：這是 LLM 服務特有的問題，也是最容易被忽略的一個。

### 5.1 問題是什麼

一個生成請求可能跑 30 秒。pod 被刪除時，如果 SIGTERM 直接送達，那個請求就斷在半路 —— 使用者看到的是回應到一半突然中斷。

更隱蔽的問題：**「從 Service endpoint 移除」和「送 SIGTERM」是兩條平行的非同步流程，沒有先後保證。** kube-proxy 要時間更新 iptables 規則，這段期間新請求還是會被送到正在關閉的 pod。

### 5.2 做法

```yaml
terminationGracePeriodSeconds: 60    # 必須 > 最長的一次生成時間

lifecycle:
  preStop:
    exec:
      command: ["sh", "-c", "sleep 10"]
```

完整的關閉時序：

```
kubectl delete pod
   │
   ├─► 從 Service endpoint 移除（非同步，kube-proxy 要時間傳播）
   │
   └─► 執行 preStop hook ── sleep 10 ──┐
                                        │  ← 這 10 秒讓 endpoint 傳播完成
       送 SIGTERM ◄─────────────────────┘
          │
          ├─► 應用程式停止接受新請求，把手上的做完
          │
          └─► 60 秒後仍未結束 → SIGKILL（強制）
```

### 5.3 為什麼 `preStop` 要睡覺

這是全 k8s 最反直覺的設定之一。原因：**k8s 沒有提供「等 endpoint 移除完成」的機制**，`preStop` 的 sleep 是社群公認的變通做法。睡的時間要大於 kube-proxy 的傳播延遲（通常 1–5 秒，繁忙叢集更久）。

`terminationGracePeriodSeconds` 的計算：

```
grace period > preStop 的 sleep + 最長的一次生成時間 + 緩衝
60          > 10 + 30 + 20  ✓
```

我們的 workload 只生 5 個 token，30 秒非常寬鬆。但如果你之後改成生成長文本，**這個值要一起調大**，否則長請求會在快完成時被 SIGKILL。

### 5.4 驗證

```bash
# 一邊持續打請求（OrbStack 讓 Mac 直接解析 Service DNS，流量走 Service 分流）
while true; do
  curl -s -o /dev/null -w "%{http_code} " \
    http://pricer-api.pricer.svc.cluster.local:8000/price -X POST \
    -H 'Content-Type: application/json' \
    -d '{"description":"Title: Test\nCategory: Tools"}'
  sleep 0.2
done

# 另一個終端只刪「一個」pod，另一個 replica 繼續接流量
kubectl -n pricer delete $(kubectl -n pricer get pod -l app=pricer-api -o name | head -1) --wait=false
```

**成功條件：全程只看到 `200`，沒有任何 `000`（連線中斷）或 `502` / `503`。**

兩個容易做錯、而且錯了會看到 `000` 但跟 preStop 無關的地方：

- **不要用 `-l app=pricer-api` 一次刪全部。** 兩個舊 pod 同時從 endpoint 移除，新 pod 還沒通過 readiness，中間會有幾秒「沒有任何 pod 可接」的空窗。preStop 只保證「已送進來的請求做完」，填不了這個洞。要換掉全部 pod 請用 `rollout restart`（Part 11）。
- **不要用 `port-forward`。** `kubectl port-forward svc/...` 其實只綁定其中一個 pod，不經過 Service 分流；那個 pod 被刪，轉發就斷了。

想看到失敗的樣子？把 `preStop` 拿掉再做一次，你會看到零星的連線錯誤。**建議真的做一次這個對照** —— 親眼看過之後，這個設定就不會再忘記。

---

## Part 6：pricer-api 與服務間呼叫

**目的**：取代課程裡直接呼叫 Modal 的那段，並補上它缺少的 timeout 與 retry。

### 6.1 做法

image 已在 Part 4.1 build 過。之後改了 `app.py` 才需要重 build，並重啟 pod 讓它吃到新 image（tag 沒變，`IfNotPresent` 不會自己換）：

```bash
docker build -t pricer-api:dev src/pricer-api
kubectl -n pricer rollout restart deploy/pricer-api
```

### 6.2 為什麼要獨立一層

課程的 `week8/agents/specialist_agent.py` 只有 28 行，核心是：

```python
Pricer = modal.Cls.from_name("pricer-service", "Pricer")
result = self.pricer.price.remote(description)     # 沒有 timeout、沒有 retry
```

換成 k8s 之後有三件事變了：

1. **呼叫變成 HTTP**，位址從寫死的服務名變成 Service DNS：`http://model-server:8080`
2. **後端會重啟**（滾動更新、OOM、節點維護）。原版沒有任何錯誤處理，後端一重啟整條呼叫鏈就炸。`app.py` 補上了 timeout 與指數退避重試。
3. **prompt 組裝從伺服器端搬到呼叫端**。Modal 版是在伺服器裡組 prompt，但 vLLM / llama.cpp 是通用的推論伺服器，不知道你的任務模板 —— 所以模板變成呼叫端的責任。

**為什麼重試是安全的**：`temperature=0` 讓請求具冪等性，重試不會得到不同答案，也不會造成副作用。如果是有副作用的 API，盲目重試是危險的。

**為什麼分成兩個 Deployment 而不是一個**：

| | pricer-api | model-server |
|---|---|---|
| 啟動時間 | < 2 秒 | 1–3 分鐘 |
| 資源 | 128MB 記憶體 | 6GB 記憶體（Phase 3 是一張 GPU） |
| 改動頻率 | 高（業務邏輯） | 低（模型） |
| 擴縮訊號 | 請求數 | 排隊長度 |

綁在一起的話，改一行業務邏輯就要付出重載模型的數分鐘代價。

### 6.3 驗證

```bash
kubectl -n pricer port-forward svc/pricer-api 8000:8000 &

curl -s http://localhost:8000/price \
  -H 'Content-Type: application/json' \
  -d '{"description":"Title: Bosch 12V Max Cordless Drill\nCategory: Tools\nBrand: Bosch\nDescription: A compact cordless drill.\nDetails: Two batteries included."}' \
  | python3 -m json.tool
```

應該拿到 `{"price": 129.0, "raw": "129.00", "latency_ms": ...}`。

驗證 Service DNS 真的在運作：

```bash
kubectl -n pricer exec deploy/pricer-api -- \
  python3 -c "import socket; print(socket.gethostbyname('model-server'))"
```

### ⚠️ 陷阱

- **`imagePullPolicy` 必須是 `IfNotPresent`**（或 `Never`）。本機 build 的 image 不在任何 registry 上，預設的 `Always` 會讓 pod 卡在 `ImagePullBackOff`。
  ⚠️ 而且：**tag 是 `:latest` 時 k8s 會強制用 `Always`**，不管你怎麼設。這就是為什麼我們用 `:dev` 而不是 `:latest`。
- **OrbStack 的 k8s 直接吃得到 Docker 的本地 image**，不用 `kind load` 那種步驟。這是它的便利，但也代表 Phase 0.5 換到 kind 時你會多一個步驟（`kind load docker-image`）—— 別忘了。
- **prompt 模板必須逐字相同**。`app.py` 從 ConfigMap 讀 `PROMPT_QUESTION` / `PROMPT_PREFIX`，而 ConfigMap 由 `k8s/base/model.conf` 產生 —— 只有一個真實來源。

---

## Part 7：Ingress 與 streaming 的坑

### 7.1 做法

```bash
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update
helm install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace

kubectl -n pricer apply -f - <<'EOF'
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: pricer
  annotations:
    # ⚠️ 這三個 annotation 是 LLM 服務的必備品，見下方說明
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "300"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "300"
spec:
  ingressClassName: nginx
  rules:
    - host: pricer.k8s.orb.local
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: pricer-api
                port: { name: http }
EOF
```

OrbStack 有 `*.k8s.orb.local` 的萬用字元 DNS，所以不用改 hosts 檔：

```bash
curl -s http://pricer.k8s.orb.local/price -X POST \
  -H 'Content-Type: application/json' \
  -d '{"description":"Title: Test Product\nCategory: Tools"}'
```

### 7.2 為什麼那三個 annotation 是必要的

**這題被嚴重低估，但面試很愛問。**

| Annotation | 不設的後果 |
|---|---|
| `proxy-buffering: "off"` | **串流會被打壞。** nginx 預設會先把後端回應緩衝完才轉發，SSE 的 token 會累積成一大包一次吐出，使用者體驗從「逐字浮現」變成「卡很久然後一次跳出來」 |
| `proxy-read-timeout` | **預設只有 60 秒。** 長生成會在 60 秒被硬生生切斷，而且錯誤訊息出現在 nginx 而不是你的應用，很難查 |
| `proxy-send-timeout` | 同上，針對送出方向 |

**更深一層**：LLM 的長連線讓 L7 負載平衡器的基本假設失效。傳統 LB 假設連線短、可以用連線數做負載平衡。LLM 的連線可能持續數分鐘，一個「連線數最少」的 pod 可能正在處理最重的請求。這是為什麼 LLM 網關（Envoy AI Gateway、vLLM 的 router）會用 KV cache 使用率之類的指標做路由，而不是連線數。

**Gateway API 呢？** Ingress 當然也能設 timeout，但 Ingress API 本身沒有這個欄位，只能靠廠商各自定義的 annotation 字串（上面那三行就是）：換 controller 要全部重寫、值寫錯 `apply` 也不會報錯。Gateway API 把 timeout 做成 HTTPRoute 的正式欄位（`timeouts.request`），所有實作照同一份規格；retry 截至 v1.6 仍在往標準推進中。

再往上一層是 **Gateway API Inference Extension**（llm-d 建立在它之上）：用 `InferencePool` + Endpoint Picker，依各 pod 的佇列長度、KV cache、已載入的 LoRA 決定請求送哪個 pod —— 解決的是「LLM-aware 路由」，不是 annotation 問題，而且它需要 Gateway API 當地基。

⚠️ **ingress-nginx 已於 2026-03 退役**（不再有任何修補，含安全漏洞），官方建議遷移到 Gateway API。本 lab 仍用它，只因為它最簡單、教材最多，拿來練 Ingress 概念夠用；不要把它帶進正式環境。

### ⚠️ 陷阱

- **我們這個 workload 只生 5 個 token，其實不會踩到串流的坑。** 但這些設定必須現在就寫進去並理解，因為面試一定會問，而且你之後接任何 chat 類的服務就會立刻撞到。
- `port: { name: http }` 用名字而不是號碼，這樣改 port 時只要改一個地方。

---

## Part 8：可觀測性

**目的**：這一整塊是純 k8s 功夫，跟 GPU 無關。在本機做完，Phase 3 就只要換指標名稱。

### 8.1 做法

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --set prometheus.prometheusSpec.retention=6h \
  --set prometheus.prometheusSpec.resources.requests.memory=400Mi

kubectl apply -f k8s/observability/servicemonitor.yaml
```

打開 Grafana（帳號是 `admin`，密碼由 Helm 自動產生）：

```bash
kubectl -n monitoring get secret monitoring-grafana \
  -o jsonpath='{.data.admin-password}' | base64 -d; echo

kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80
# 瀏覽器開 http://localhost:3000
```

### 8.2 你該建的 dashboard 面板

llama.cpp 提供這些指標（Phase 3 的 vLLM 有語意對應的版本）：

| llama.cpp | vLLM 的對應 | 意義 |
|---|---|---|
| `llamacpp:requests_processing` | `vllm:num_requests_running` | 正在處理中 |
| `llamacpp:requests_deferred` | `vllm:num_requests_waiting` | **排隊中 ← 擴縮訊號** |
| `llamacpp:kv_cache_usage_ratio` | `vllm:kv_cache_usage_perc` | KV cache 用了多少 |
| `llamacpp:prompt_tokens_total` | `vllm:prompt_tokens_total` | prefill 的 token 累計 |
| `llamacpp:tokens_predicted_total` | `vllm:generation_tokens_total` | decode 的 token 累計 |
| `llamacpp:tokens_predicted_seconds_total` | — | decode 累計耗時 |

四個該建的面板：

```promql
# 1. 吞吐（output tokens/s）
rate(llamacpp:tokens_predicted_total[1m])

# 2. 佇列深度 —— 這是最重要的一張，它是飽和度的直接訊號
llamacpp:requests_deferred

# 3. KV cache 使用率 —— 接近 1 代表你的並發上限到了
llamacpp:kv_cache_usage_ratio

# 4. 平均 decode 速度（秒/token）
rate(llamacpp:tokens_predicted_seconds_total[5m])
  / rate(llamacpp:tokens_predicted_total[5m])
```

### 8.3 為什麼要看佇列而不是使用率

**這是這個 lab 最重要的觀念之一。**

「使用率」是個容易騙人的指標。一個服務可以 CPU 只用 20% 但請求排到天邊（瓶頸在 GPU 或記憶體頻寬）。反過來，GPU 使用率 100% 也不代表它在做有用的事（Phase 3 會用 DCGM 證明這件事）。

**佇列深度是飽和度的直接證據**：有東西在等，就是能力不足。這是排隊理論的基本結論，也是為什麼 Google SRE 的四大黃金訊號裡有「飽和度」這一項而不是「使用率」。

### 8.4 驗證

```bash
kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090:9090
# 瀏覽器開 http://localhost:9090/targets
# 找 pricer/model-server，狀態應該是 UP
```

### ⚠️ 陷阱

- **ServiceMonitor 建了但 Prometheus 沒抓** —— 這是第一名的問題，原因幾乎都是 label 不對。kube-prometheus-stack 預設只認 `release` label 等於自己 Helm release 名稱的 ServiceMonitor。查你的實際值：
  ```bash
  kubectl get prometheus -A -o jsonpath='{.items[0].spec.serviceMonitorSelector}{"\n"}'
  ```
- **`endpoints.port` 填的是 Service 裡「port 的名字」，不是號碼。** 這就是為什麼 `model-server.yaml` 的 Service 一定要給 port 命名。
- **ServiceMonitor 的 `selector` 比對的是 Service 的 label，不是 Deployment 的。** 兩者剛好同名時特別容易搞混。
- **kube-prometheus-stack 在 16GB 的機器上很重。** 上面刻意把 retention 設成 6 小時、記憶體 400Mi。做完這一階段可以 `helm uninstall monitoring` 把資源還回來，需要時再裝。

---

## Part 9：HPA —— CPU 為什麼是錯的

### 9.1 先看錯的做法

```bash
kubectl -n pricer autoscale deploy/model-server --cpu-percent=70 --min=1 --max=4
kubectl -n pricer get hpa -w
```

壓一下負載，你會看到 **HPA 完全不動**。CPU 使用率不會到 70%，因為瓶頸不在 CPU（本機是記憶體頻寬，GPU 上是 GPU）。

```bash
kubectl -n pricer delete hpa model-server
```

**把這個失敗實際做一次。** 「我試過 CPU HPA，它不會觸發，因為 LLM 的瓶頸不是 CPU」這句話，自己看過之後講起來完全不一樣。

### 9.2 對的做法

```bash
helm install prometheus-adapter prometheus-community/prometheus-adapter \
  -n monitoring -f k8s/observability/prometheus-adapter-values.yaml

# 確認 custom metrics API 出現了
kubectl get --raw /apis/custom.metrics.k8s.io/v1beta1 \
  | python3 -m json.tool | grep -i pricer

kubectl apply -f k8s/observability/hpa.yaml
kubectl -n pricer get hpa model-server -w
```

### 9.3 為什麼 `behavior` 那一段是必要的

`k8s/observability/hpa.yaml` 裡最重要的其實是 `behavior`：

```yaml
behavior:
  scaleUp:
    stabilizationWindowSeconds: 180   # 窗口過濾尖峰
    policies:
      - type: Pods
        value: 1                      # policy 限制速度：
        periodSeconds: 180            #   任何 180 秒內最多 +1
```

**核心問題：控制迴路的延遲遠大於取樣週期。**

HPA 預設每 15 秒評估一次，但新的 model-server pod 要 1–3 分鐘才會 ready。不設穩定視窗的話：

```
t=0s    佇列變長          → 擴到 2
t=15s   新 pod 還在載模型，佇列沒變短 → 擴到 3
t=30s   還是沒變短        → 擴到 4（已達上限）
t=3min  四個 pod 同時就緒，但需求早就過了 → 浪費 + 接著一路縮回去
```

這是控制理論裡的**積分飽和**。解法分兩層：

- **窗口過濾尖峰**：`stabilizationWindowSeconds` 取窗口內建議值的最小值（縮容取最大值），需求要持續整個窗口才會動，短暫尖峰直接忽略。
- **policy 限制速度**：`policies` 規定任何 `periodSeconds` 內最多變動 `value` 個 pod。**擋住上面連續擴容的是這一層** —— 光靠窗口不夠：t=180s 擴到 2 之後，窗口內的建議值仍然都 ≥3，下一輪照樣會擴。period 設成 ≥ 冷啟動時間，新 pod ready 之前就不會再加下一個。

**縮容要更保守**（我們設 600 秒），因為縮錯的代價（再等 3 分鐘冷啟動）遠高於多留一個 pod 的成本。

**能講出「窗口過濾尖峰；因為冷啟動 3 分鐘，所以 policy 的 period 設成 180 秒、一次只加一個」，比背出 HPA 有哪些欄位有價值得多。**

### 9.4 驗證

```bash
# 製造負載 —— 壓測直接打 model-server：measure.py 打的是 /v1/completions，
# Ingress 後面的 pricer-api 只有 /price，打 pricer.k8s.orb.local 會 404。
# HPA 看的是 model-server 的排隊長度，繞過 pricer-api 不影響測試。
uv run --with datasets eval/measure.py \
  --base-url http://model-server.pricer.svc.cluster.local:8080 --model pricer \
  --size 200 --workers 30 --label "hpa-load-test"

# 另一個終端觀察
kubectl -n pricer get hpa model-server -w
kubectl -n pricer describe hpa model-server | tail -20
```

成功條件：`describe` 的 Events 裡看到 `SuccessfulRescale`，且 `TARGETS` 欄位顯示的是實際的佇列長度而不是 `<unknown>`。**看到 `SuccessfulRescale` 就算過**，第二個 pod 不需要跑起來。

**預期現象：HPA 觸發了，但 scheduler 放不下。** 新開的 model-server 會一直卡在 `Pending`：

```
$ kubectl -n pricer get events --field-selector reason=FailedScheduling
0/1 nodes are available: 1 Insufficient memory.
```

OrbStack VM 預設 8 GiB，一個 model-server 就 request 5Gi，第二個排不進去。k9s 裡看到 `stream closed: EOF` 是因為容器從沒啟動過、沒有 log，不是崩潰。

這正是要學的：**HPA 只改「期望 replica 數」，它生不出機器。** 容量不夠時 pod 就是 Pending；真實叢集裡補節點的是 Cluster Autoscaler / Karpenter（Phase 4）。

測完把 Pending 的 pod 清掉：

```bash
kubectl -n pricer delete hpa model-server
kubectl -n pricer scale deploy/model-server --replicas=1
```

### ⚠️ 陷阱

- **`<unknown>` 是最常見的症狀**，代表 prometheus-adapter 沒有正確暴露指標。排查順序：
  1. `kubectl get --raw /apis/custom.metrics.k8s.io/v1beta1` 有沒有你的指標名
  2. Prometheus 裡直接查那個 series 有沒有資料
  3. `kubectl -n monitoring logs deploy/prometheus-adapter` 看 `seriesQuery` 的錯誤
- **HPA 的 `Pods` 類型指標是「平均每個 pod」**，不是總和。設 `averageValue: 2` 表示「平均每個 pod 排 2 個」，不是「總共排 2 個」。
- 本機的擴容數字**不要當真**。OrbStack 預設 8 GiB 的 VM 連第 2 個 replica 都放不下（見 9.4 的預期現象）。這裡只練機制，真實數字到 Phase 3。

---

## Part 10：Kustomize —— 讓這一切能搬到 GPU 上

### 10.1 結構

```
k8s/
├── base/                    ← 兩個 Phase 共用，不含任何 GPU 相關設定
│   ├── model.conf           ← 模型座標的唯一真實來源
│   ├── namespace.yaml
│   ├── model-server.yaml
│   ├── pricer-api.yaml
│   └── kustomization.yaml
└── overlays/
    ├── local/               ← Phase 0：llama.cpp、CPU、小資源
    └── gpu/                 ← Phase 3：vLLM、nvidia.com/gpu（之後建）
```

```bash
kubectl kustomize k8s/overlays/local     # 先看產生什麼，再 apply
kubectl apply -k k8s/overlays/local
```

### 10.2 為什麼 base 裡不放 GPU 設定

**因為 base 應該是「兩邊都成立」的最大公因數。** 如果 base 裡寫了 `nvidia.com/gpu: 1`，本機就 apply 不了，你得靠 overlay 把它「拿掉」—— 而 Kustomize 的移除語法比新增語法難用得多，也難讀得多。

**原則：base 放共通的，overlay 只做「加法」。**

### 10.3 configMapGenerator 的雜湊後綴

```yaml
configMapGenerator:
  - name: model-config
    envs: [model.conf]
generatorOptions:
  disableNameSuffixHash: false     # 刻意保留雜湊
```

產生出來的名字是 `model-config-kmk2b84265`。**這個雜湊是刻意保留的。**

設定內容一改 → 雜湊變 → Deployment 的 pod template 跟著變 → **自動觸發滾動更新**。

關掉的話，你改了 ConfigMap 卻沒有任何 pod 會重新讀它 —— 這是 k8s 的經典陷阱：ConfigMap 更新**不會**自動重啟 pod（以環境變數注入的更是連掛載都不會更新）。很多人是在「我明明改了設定但服務行為沒變」debug 半天之後才學到這件事。

### ⚠️ 陷阱 —— 你已經踩過的那個

`model.conf` 為什麼住在 `k8s/base/` 而不是 `config/`？

**Kustomize 的 load restrictor 不准 kustomization 讀取自己目錄以外的檔案。** 這是為了讓 kustomization 可以被安全地遠端引用（不會偷偷讀到你 repo 的其他地方）。

```
error: security; file '.../config/model.conf' is not in or below '.../k8s/base'
```

繞過的方法是 `--load-restrictor LoadRestrictionsNone`，但那是個會傳染的壞習慣 —— 加了之後每個人每次 build 都要記得加。讓設定檔住在 base 裡是零成本的正解。

**Helm 還是 Kustomize？**（高頻面試題）
- **Kustomize** 適合自己的應用：沒有模板語言、輸出可預測、`kubectl` 內建
- **Helm** 適合安裝第三方元件：有版本管理、依賴、`helm rollback`
- 本 lab 兩個都用，而且是按這個原則分的：自己的東西用 Kustomize，ingress-nginx / kube-prometheus-stack / GPU Operator 用 Helm。**能講出「我按這個原則分」比選邊站有說服力。**

---

## Part 11：滾動更新

### 11.1 做法

```bash
# 一邊持續打請求監看是否斷線
while true; do
  curl -s -o /dev/null -w "%{http_code} " http://pricer.k8s.orb.local/price \
    -X POST -H 'Content-Type: application/json' \
    -d '{"description":"Title: Test\nCategory: Tools"}'
  sleep 0.5
done

# 另一個終端觸發更新
kubectl -n pricer rollout restart deploy/model-server
kubectl -n pricer rollout status deploy/model-server
```

### 11.2 為什麼是 `maxSurge: 1` / `maxUnavailable: 0`

```yaml
strategy:
  rollingUpdate:
    maxSurge: 1          # 先開新的
    maxUnavailable: 0    # 舊的一個都不准先關
```

一般服務啟動只要幾百毫秒，`maxUnavailable: 1` 的短暫容量下降沒人會注意到。

**LLM 服務啟動要 1–3 分鐘。** 如果先關舊的再開新的，就是 1–3 分鐘的完全斷線。所以必須「先開新的、確認就緒、才關舊的」。

**代價：rollout 期間需要雙倍的資源。** Phase 3 只有一張 GPU 時，這個設定會讓 rollout **永遠卡住** —— 新 pod 永遠等不到 GPU，因為舊 pod 還佔著，而舊 pod 又要等新 pod 就緒才會被關掉。

**本機其實已經會卡住了。** OrbStack VM 預設 8 GiB，model-server request 5Gi，新 pod 一樣 `Pending`（`Insufficient memory`），`rollout status` 永遠等不到。任何改到 Pod template 的操作都會中：`rollout restart`、改 args 後 `kubectl apply -k`。

單節點上的兩個解法：

```bash
# (a) 接受停機：先縮到 0 再套用，會有 1–3 分鐘完全斷線
kubectl -n pricer delete hpa model-server --ignore-not-found   # HPA 會把 replica 拉回去
kubectl -n pricer scale deploy/model-server --replicas=0
kubectl apply -k k8s/overlays/local                            # manifest 的 replicas: 1 會開回 1 個
```

**(b) 加記憶體**：OrbStack → Settings → System → Memory limit 調到約 12 GiB，才放得下兩個 5Gi 的 request 加上監控元件。16 GiB 的 Mac 這樣會很緊，只在要練 11.3 的「全程 200」時開。

用 (a) 做 11.1 會看到斷線，這不是失敗，是提早體驗 Phase 3 單卡的兩難。

**這是 on-prem 最真實的兩難**，Phase 3 會正面處理它（三個選項：接受停機、多一張卡、或用 time-slicing 假裝有多張卡）。

### 11.3 驗證

成功條件：整個 rollout 過程中，請求全程 `200`。迴圈要在 `restart` / `undo` **之前**就開始跑，一直到舊 pod 消失才停。

**前提：OrbStack 記憶體要先調到約 12 GiB（11.2 的做法 b）**，預設 8 GiB 放不下 surge 出來的新 pod。確認：

```bash
kubectl get node orbstack -o jsonpath='{.status.allocatable.memory}{"\n"}'   # 約 12295768Ki
```

```bash
kubectl -n pricer rollout history deploy/model-server
kubectl -n pricer rollout undo deploy/model-server     # 練一次回滾
```

- `rollout restart` 也會產生新 revision：它是在 Pod template 加 `restartedAt` annotation，template 變了就是新的 ReplicaSet。
- `undo` 回去的那一版會**重新編號成最新**，舊號碼從 history 消失，這是正常的。
- ⚠️ `undo` 前先用 `rollout history --revision=N` 看清楚目標版本。回到沒有 `--cache-ram 0` 的舊版，OOM 會回來。

實測（2026-09-27，12 GiB）：`restart` 與 `undo` 全程 `200`，新 pod 約 70 秒 ready。測完記得把記憶體調回 8 GiB。

---

## Part 12：三個刻意故障演練

**目的**：這一節的產出是 `POSTMORTEMS.md`，而它是整個 lab 面試價值最高的東西。

**為什麼**：面試官聽過太多「我部署過 k8s」。能拿出三份 post-mortem（現象 → 排查路徑 → 根因 → 修法 → 預防）證明的是**你排查過**，不只是部署過。而且這三個故障都是 LLM 服務的典型死法。

每個演練都照這個格式寫進 `POSTMORTEMS.md`：

```markdown
## 事故 N：<一句話標題>
**現象**：（你看到什麼，貼實際的指令輸出）
**排查路徑**：（你依序查了什麼，包含查錯方向的那些）
**根因**：（真正的原因）
**修法**：（實際的 diff）
**預防**：（怎麼讓它不再發生）
```

### 演練 1：記憶體上限設太低 → OOMKilled

```bash
kubectl -n pricer set resources deploy/model-server --limits=memory=2Gi
kubectl -n pricer get pods -l app=model-server -w
```

**你會看到**：pod 反覆 `CrashLoopBackOff`，`RESTARTS` 一直爬。

**排查**：
```bash
kubectl -n pricer describe pod -l app=model-server | grep -A5 "Last State"
# Last State: Terminated
#   Reason: OOMKilled
#   Exit Code: 137          ← 137 = 128 + 9 (SIGKILL)
```

**要理解的重點**：exit code 137 就是 OOMKilled 的指紋。而且 **log 裡什麼都不會有** —— 行程是被核心直接砍的，沒機會寫任何東西。這就是為什麼要看 `describe` 的 `Last State` 而不是 `kubectl logs`。

**LLM 專屬的難處**：模型佔用的記憶體在**載入完成的瞬間**達到峰值，之後才穩定。limit 必須容納那個峰值，不是穩定值。這也是為什麼 vLLM 的 `--gpu-memory-utilization` 要預先劃定 —— 它就是在把這件事變成可預測的。

**還原**：
```bash
kubectl -n pricer set resources deploy/model-server --limits=memory=8Gi
```

### 演練 2：startupProbe 時間不夠 → 無限 CrashLoop

```bash
kubectl -n pricer patch deploy/model-server --type=json -p='[
  {"op":"replace","path":"/spec/template/spec/containers/0/startupProbe/failureThreshold","value":2}
]'
kubectl -n pricer get pods -l app=model-server -w
```

**你會看到**：pod 每 20 秒就被殺一次，永遠起不來。而且 —— **這是最陰險的地方 —— log 看起來完全正常**，它只是還在載模型而已。

**排查**：
```bash
kubectl -n pricer describe pod -l app=model-server | grep -i "startup probe"
# Warning  Unhealthy  ... Startup probe failed: ...
kubectl -n pricer logs -l app=model-server --previous | tail
# 你會看到它正在正常載入，被砍在半路
```

**要理解的重點**：容忍時間 = `failureThreshold × periodSeconds`。這裡是 `2 × 10 = 20` 秒，但模型要 60 秒以上。**這個錯誤在本機很容易發現，但在 Phase 3 的 GPU 上更嚴重** —— vLLM 還要編 CUDA graph，啟動時間更長，而且你是在按小時計費。

**預防**：部署新模型時，先用 `kubectl logs -f` 手動量一次冷啟動時間，再把 `failureThreshold` 設成那個值的 2 倍以上。

**還原**：
```bash
kubectl -n pricer patch deploy/model-server --type=json -p='[
  {"op":"replace","path":"/spec/template/spec/containers/0/startupProbe/failureThreshold","value":60}
]'
```

### 演練 3：initContainer 失敗 → 卡在 Init

```bash
# 把 adapter 從 PVC 移走，模擬「模型檔沒有正確送達」
kubectl -n pricer apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata: { name: breaker }
spec:
  containers:
    - name: shell
      image: busybox:1.37
      command: ["sh","-c","mv /models/pricer-lora.gguf /models/pricer-lora.gguf.bak && sleep 30"]
      volumeMounts: [{ name: models, mountPath: /models }]
  volumes:
    - name: models
      persistentVolumeClaim: { claimName: model-weights }
EOF

kubectl -n pricer delete pod -l app=model-server
kubectl -n pricer get pods -l app=model-server -w
```

**你會看到**：`Init:Error` 或 `Init:CrashLoopBackOff`，**主容器完全沒有啟動**。

**排查**：
```bash
kubectl -n pricer describe pod -l app=model-server | grep -A10 "Init Containers"
# 關鍵：要指定 initContainer 的名字才看得到它的 log
kubectl -n pricer logs -l app=model-server -c fetch-base-model
```

**要理解的重點**：
- initContainer 的 log **不會**出現在 `kubectl logs <pod>` 的預設輸出，一定要加 `-c <initContainer名>`。不知道這件事的話會查很久。
- 我們的 initContainer 是**刻意設計成找不到 adapter 就失敗**的。如果它靜靜地跳過，服務會正常起來、正常回答、但 MAE 是 base 的 $200 左右而不是 $40。**開不起來的錯誤比安靜答錯的錯誤好一百倍**，這是一個真實的設計決策。

**還原**：
```bash
kubectl -n pricer delete pod breaker --ignore-not-found
kubectl -n pricer apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata: { name: fixer }
spec:
  containers:
    - name: shell
      image: busybox:1.37
      command: ["sh","-c","mv /models/pricer-lora.gguf.bak /models/pricer-lora.gguf && sleep 10"]
      volumeMounts: [{ name: models, mountPath: /models }]
  volumes:
    - name: models
      persistentVolumeClaim: { claimName: model-weights }
EOF
sleep 20 && kubectl -n pricer delete pod fixer --ignore-not-found
kubectl -n pricer rollout restart deploy/model-server
```

---

## Phase 0 驗收清單

- [x] 本機 `llama-server` 跑出第一個 MAE，且落在 $35–$50 之間
- [x] 數字已填進 `RESULTS.md`
- [x] 已驗證 OrbStack 的 CNI **是否**強制執行 NetworkPolicy（結果記下來）
- [x] `kubectl apply -k k8s/overlays/local` 後所有 pod 都 `Running` 且 `restartCount` 為 0
- [x] `curl http://pricer.k8s.orb.local/price` 拿得到估價
- [x] 刪 pod 時全程 `200`，沒有連線中斷（優雅關機生效）
- [x] Prometheus 的 `/targets` 看得到 `model-server` 是 `UP`
- [x] Grafana 上四張面板都有資料
- [x] CPU HPA **不會觸發**（親眼確認過）
- [x] custom metrics HPA 在壓測時實際擴容（`SuccessfulRescale` 事件）
- [x] `rollout restart` 過程中服務不中斷
- [x] 三份 post-mortem 已寫進 `POSTMORTEMS.md`

---

## 收工

做完之後把本機資源還回來（你會需要記憶體給 Phase 0.5 的 kind）：

```bash
helm uninstall monitoring -n monitoring
helm uninstall prometheus-adapter -n monitoring
kubectl delete -k k8s/overlays/local
orb stop k8s            # ⚠️ 16GB 的機器不要同時開 OrbStack k8s 和 kind
```

模型檔留著（`models/` 已 gitignore），Phase 0.5 還要用。

---

## 下一站

→ [`03-phase0.5-multinode.md`](03-phase0.5-multinode.md)：用 kind 開三節點，補掉單節點練不到、而且會給你錯誤直覺的部分：真正的排程、`drain` 與 PDB、RWX、MetalLB、以及 on-prem 的資源治理。一樣 $0。
