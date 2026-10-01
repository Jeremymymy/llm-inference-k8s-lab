# LLM Inference on Kubernetes 實驗室 — 06：Phase 3，GPU 上的 Kubernetes

> 成本：**約 $12**（同一台 g6.xlarge，再累積開機 15 小時）。**不開 EKS，所以沒有額外的控制平面費用。**
> 產出：Phase 0/0.5 的 k8s 成果接上真 GPU，加上三個只有真 GPU 才做得到的實驗。

---

## 這一階段的目的

**把前面兩個零成本階段的成果，接上真正的 GPU。**

**為什麼是 k3s 而不是 EKS**：

| | k3s（單機） | EKS |
|---|---|---|
| 控制平面成本 | $0 | $0.10/hr（約 $73/月） |
| GPU device plugin | 一樣 | 一樣 |
| `nvidia.com/gpu` 資源 | 一樣 | 一樣 |
| time-slicing | 一樣 | 一樣 |
| DCGM 監控 | 一樣 | 一樣 |
| **跟 on-prem 的相似度** | **高** | 低（雲端特有的東西比較多） |

**GPU + k8s 的核心知識，在單機 k3s 上 100% 學得到。** 而且你要應徵的是 on-prem 職缺 —— 自管叢集本來就是那個場景。EKS 留在 [`07-phase4-eks.md`](07-phase4-eks.md) 當對照表。

**十個部分：**

1. 裝 k3s 與 NVIDIA Container Toolkit
2. GPU Operator 與 `nvidia.com/gpu`
3. 把模型與憑證放進叢集
4. 部署 gpu overlay
5. `/dev/shm` 的坑
6. 單卡滾動更新的死結
7. GPU time-slicing
8. DCGM —— 「GPU util 100%」是騙人的
9. 可觀測性與 HPA 接上真指標
10. Helm 實務

---

## 精簡路線（本 lab 採用）

完整做完大約要 10–16 小時動手時間。**精簡路線只做「只有真 GPU 才做得到」的部分**，約 6–8 小時、GPU 費用約 $6–8，拿到的是這個 Phase 最有價值的四樣東西：GPU 接進 k8s 的完整鏈路、單卡 rollout 死結的停機數字、time-slicing 對照表、DCGM 三指標。

| Part | 精簡路線 | 理由 |
|---|---|---|
| 1. k3s + Container Toolkit | ✅ 必做 | 排查順序的第二層，後面全靠它 |
| 2. GPU Operator | ✅ 必做 | `nvidia.com/gpu`、time-slicing、DCGM 都從這裡來 |
| 3. 模型與憑證進叢集 | ✅ 必做 | |
| 4. 部署 gpu overlay + MAE 對齊 | ✅ 必做 | **證明 k8s 這層沒改變行為**，MAE 要跟 Phase 1 一樣 |
| 5. `/dev/shm` | 📖 讀就好 | base 已經擋掉了；知道原因、能講就夠，不必故意弄壞 |
| 6. 單卡 rollout 死結 | ✅ 必做 | 量出停機秒數，這是選項 A 的真實成本 |
| 7. time-slicing | ✅ 必做 | 1 / 2 pod 的吞吐、p99、MAE 表 —— 整個 lab 最有說服力的一張表 |
| 8. DCGM | ✅ 必做 | `GPU_UTIL` vs `SM_ACTIVE` vs `PIPE_TENSOR_ACTIVE`，區辨度最高 |
| 9. 可觀測性 + HPA | ⏭️ 可選 | 要裝整套 Prometheus stack，最花時間；HPA 的抽象層設計 Phase 0 已經練過 |
| 10. Helm 實務 | 📖 讀就好 | 那幾個指令與陷阱讀過、能講就夠 |

**bench pod 在 Part 6 建立**，Part 7 的對照表和 Part 8 的 DCGM 觀察都用它產生壓測負載（指令見 Part 9.3 的 `kubectl exec bench -- vllm bench serve ...`，只借那一段，不用裝 Prometheus）。做完 Part 8 就 `kubectl -n pricer delete pod bench`。

**驗收清單照做，但跳過 HPA 那兩項**（「HPA 實際擴容」、「HPA 門檻已校正」），並在 `KNOWN-GAPS.md` 誠實記一筆：「HPA 接 vLLM 指標只做到設計，未在 GPU 上實跑」。

---

## Part 1：裝 k3s 與 NVIDIA Container Toolkit

### 1.1 做法

```bash
# 在 Phase 1 那台 g6.xlarge 上
nvidia-smi          # DLAMI 已經有驅動了，先確認

# 1) NVIDIA Container Toolkit（讓容器看得到 GPU）
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit

# 2) 才裝 k3s —— 順序很重要，見下方
curl -sfL https://get.k3s.io | sh -

sudo chmod 644 /etc/rancher/k3s/k3s.yaml
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl get nodes
```

### 1.2 為什麼順序是「先 toolkit 再 k3s」

**k3s 在啟動時掃描系統上有哪些容器 runtime，然後自動產生 containerd 的設定。**

如果 NVIDIA Container Toolkit 是在 k3s 之後才裝的，k3s 產生的 containerd 設定裡就不會有 `nvidia` runtime，你得手動改設定檔再重啟 k3s。

先裝 toolkit，k3s 會自動幫你建好 `nvidia` 這個 RuntimeClass。

**但「有 RuntimeClass」不等於「pod 會用它」。** k3s 的**預設** runtime 仍然是 runc，pod 要明寫 `runtimeClassName: nvidia` 才會走 NVIDIA runtime。gpu overlay 已經加了這一行（見 Part 4.2）。

### 1.3 驗證

```bash
kubectl get runtimeclass
# 應該看到 nvidia

# 直接在容器裡確認看得到 GPU
sudo ctr image pull docker.io/nvidia/cuda:12.4.0-base-ubuntu22.04
sudo ctr run --rm --gpus 0 \
  docker.io/nvidia/cuda:12.4.0-base-ubuntu22.04 gputest nvidia-smi
```

### ⚠️ 陷阱

- **這是「由下往上」排查順序的第二層。** 如果這一步失敗，不要往上去查 k8s —— 問題在 runtime 層。Phase 1 已經用 `docker run --gpus all` 驗過第一層了，所以你知道驅動是好的。
- **k3s 預設會裝 Traefik 當 ingress**。跟 Phase 0 用的 ingress-nginx 不同。要嘛接受（改 `ingressClassName: traefik`），要嘛 `--disable traefik` 再自己裝 nginx。**這是「在我機器上可以跑」的另一個來源** —— ingress annotation 是廠商特定的，nginx 的那些 `proxy-buffering` annotation 在 Traefik 上完全無效。

---

## Part 2：GPU Operator 與 `nvidia.com/gpu`

### 2.1 做法

```bash
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm repo update

helm install gpu-operator nvidia/gpu-operator \
  -n gpu-operator --create-namespace \
  --set driver.enabled=false \
  --set toolkit.enabled=false
```

### 2.2 為什麼 `driver.enabled=false` 和 `toolkit.enabled=false`

**因為 DLAMI 已經裝好驅動了，Part 1 也已經裝好 toolkit 了。**

GPU Operator 預設會自己管理驅動（在容器裡編譯核心模組）。如果系統上已經有驅動，兩者會打架 —— 症狀是 pod 反覆 crash 或整台機器的 GPU 變得不可用。

**那為什麼還要 GPU Operator，不直接裝 device plugin 就好？**

| | 只裝 device plugin | GPU Operator（關掉驅動管理） |
|---|---|---|
| `nvidia.com/gpu` 資源 | ✅ | ✅ |
| **DCGM exporter（監控）** | ❌ 要另外裝 | ✅ **內建** |
| Node Feature Discovery（自動打標籤） | ❌ | ✅ |
| time-slicing 設定 | ✅ | ✅ |

**Part 8 的 DCGM 是這個 Phase 最有價值的東西之一**，GPU Operator 直接附帶，所以用它比較划算。

### 2.3 驗證

```bash
kubectl -n gpu-operator get pods
kubectl describe node | grep -A8 "Capacity:"
# nvidia.com/gpu: 1        ← 這一行出現就成功了

kubectl get nodes -o jsonpath='{.items[0].metadata.labels}' | tr ',' '\n' | grep nvidia
# nvidia.com/gpu.present=true  ← gpu overlay 的 nodeSelector 靠這個
```

**排查順序的第三層通過了。** 從 `nvidia-smi` → `docker run --gpus all` → `nvidia.com/gpu` 出現在 node capacity → 下一步是 pod 真的拿到卡。

---

## Part 3：把模型與憑證放進叢集

```bash
kubectl apply -f k8s/base/namespace.yaml

# 憑證做成 Secret（從檔案建，避免手動複製貼上）
kubectl -n pricer create secret generic hf-token \
  --from-file=token=$HOME/private/hf-token

# LoRA adapter 放進 PVC（做法同 Phase 0 Part 3）
kubectl -n pricer apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: model-weights }
spec:
  accessModes: ["ReadWriteOnce"]
  resources: { requests: { storage: 60Gi } }
EOF

kubectl -n pricer apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata: { name: loader }
spec:
  containers:
    - name: shell
      image: busybox:1.37
      command: ["sleep","3600"]
      volumeMounts: [{ name: models, mountPath: /models }]
  volumes:
    - name: models
      persistentVolumeClaim: { claimName: model-weights }
EOF

kubectl -n pricer wait --for=condition=Ready pod/loader --timeout=120s
kubectl -n pricer cp /home/ubuntu/adapter loader:/models/adapter
kubectl -n pricer delete pod loader
```

**為什麼 PVC 要 60Gi**：不只放 adapter，還要放 HF cache（`HF_HOME=/models/hf`）。bf16 權重 6.4GB，加上你在 Phase 2 做的各種量化版本，很快就會超過 20GB。

**為什麼 HF cache 要放 PVC**：不然每次 pod 重啟都要重新從 HF 下載 6.4GB。在 k8s 裡 pod 重啟是家常便飯（滾動更新、OOM、節點維護），每次等 5 分鐘不可接受。

---

## Part 4：部署 gpu overlay

### 4.1 做法

```bash
# 把 lab repo 弄上機器（或 git clone）
cd ~/llm-inference-k8s-lab

docker build -t pricer-api:dev src/pricer-api
sudo k3s ctr images import <(docker save pricer-api:dev)   # k3s 用自己的 image store

kubectl kustomize k8s/overlays/gpu | less     # 先看再 apply
kubectl apply -k k8s/overlays/gpu

kubectl -n pricer get pods -w
```

### 4.2 base 跟 overlay 的差異一覽

**這是整個 lab 的關鍵驗證點：Phase 0 寫的東西到底沿用了多少。**

| 項目 | base（Phase 0 就寫好） | gpu overlay 加的 |
|---|---|---|
| 三種 probe 的結構 | ✅ | 只調 `failureThreshold`（90 vs 60） |
| `preStop` + grace period | ✅ | 不變 |
| `/dev/shm` 的 emptyDir | ✅ | 不變（Phase 0 就預留了） |
| securityContext | ✅ | 不變 |
| Service / ConfigMap / PVC | ✅ | 不變 |
| pricer-api 整個 | ✅ | 不變 |
| image 與 args | llama.cpp | **vLLM** |
| `nvidia.com/gpu` | — | **新增** |
| `runtimeClassName: nvidia` | — | **新增** |
| toleration + nodeSelector | — | **新增** |
| HF_TOKEN 的 secretKeyRef | — | **新增** |

**大部分東西都沒變。** 這就是 Phase 0 花時間在零成本環境把骨架做對的回報。

### 4.3 驗證

```bash
# pod 真的拿到卡了嗎（排查順序的第四層）
kubectl -n pricer exec deploy/model-server -- nvidia-smi

kubectl -n pricer port-forward svc/model-server 8000:8080 &   # 本機 8000 → Service 的 8080（再轉到容器的 8000）
curl -s localhost:8000/v1/models | python3 -m json.tool
```

**然後跑一次正確性驗證** —— 這是最重要的一步：

```bash
# 本機（透過 SSH 通道）
uv run --with datasets eval/measure.py \
  --base-url http://localhost:8000 --model pricer --size 200 \
  --label "phase3-k3s-vllm-bf16"
```

**MAE 應該跟 Phase 1 的 Docker 版完全一樣。** 如果不一樣，代表 k8s 這一層改變了什麼 —— 最常見的是請求打到了 `base` 而不是 `pricer`（pricer-api 的 `MODEL_NAME`、或 args 裡的 `--served-model-name` / `--lora-modules` 名稱對不上）。

### ⚠️ 陷阱：排程成功，但容器裡沒有 GPU

症狀：pod 被排到節點上（代表 `nvidia.com/gpu` 分配成功），但 `kubectl exec ... nvidia-smi` 報 `executable file not found`，或 vLLM 的 log 說找不到 CUDA / `No CUDA GPUs are available`。

原因：**少了 `runtimeClassName: nvidia`**。device plugin 只負責「記帳」（告訴 scheduler 這張卡分給誰了），真正把 GPU 裝置和驅動函式庫塞進容器的是 NVIDIA container runtime。pod 沒指定 RuntimeClass 就會用 k3s 預設的 runc，於是帳上有卡、容器裡沒有。

```bash
kubectl -n pricer get pod -l app=model-server -o jsonpath='{.items[0].spec.runtimeClassName}'
# 應該印出 nvidia
```

另一種解法是裝 k3s 時加 `--default-runtime nvidia`，讓所有 pod 都走 NVIDIA runtime。本 lab 選擇在 pod 上明寫，因為意圖比較清楚，而且不會影響其他不需要 GPU 的 pod。

---

## Part 5：`/dev/shm` 的坑

**這個坑 base 已經幫你擋掉了，但你要知道為什麼。**

```yaml
volumes:
  - name: dshm
    emptyDir:
      medium: Memory
      sizeLimit: 2Gi
```

**k8s 給容器的 `/dev/shm` 預設只有 64MB。** vLLM 用共享記憶體做行程間通訊，負載一上來就會爆。

| 環境 | 怎麼解 |
|---|---|
| Docker | `--ipc=host` 或 `--shm-size=2g` |
| **Kubernetes** | **`emptyDir: {medium: Memory}` 掛在 `/dev/shm`** |

**想看它壞掉的樣子？**（建議做一次，這是 vLLM 在 k8s 上最常見的第一個 crash）

```bash
kubectl -n pricer patch deploy/model-server --type=json -p='[
  {"op":"remove","path":"/spec/template/spec/volumes/1"},
  {"op":"remove","path":"/spec/template/spec/containers/0/volumeMounts/1"}
]'
# 壓一下負載，看它怎麼死
```

**⚠️ `medium: Memory` 的 emptyDir 會算進容器的記憶體 limit。** 設 2Gi 的話，記憶體 limit 要留出這 2Gi，否則會變成 OOMKilled。這兩個設定必須一起調。

---

## Part 6：單卡滾動更新的死結

**目的**：正面處理 Phase 0 Part 11 埋下的那個問題。

### 6.1 讓它發生

```bash
kubectl -n pricer rollout restart deploy/model-server
kubectl -n pricer get pods -w
```

**你會看到新 pod 卡在 `Pending`，而 rollout 永遠不會完成。**

```bash
kubectl -n pricer describe pod -l app=model-server | grep -A5 FailedScheduling
# 0/1 nodes are available: 1 Insufficient nvidia.com/gpu
```

### 6.2 為什麼無解

```
strategy: maxSurge: 1, maxUnavailable: 0
   ↓
新 pod 要先就緒，舊 pod 才能關
   ↓
但只有一張卡，舊 pod 佔著
   ↓
新 pod 永遠排不進去 → 死結
```

**根本原因：`nvidia.com/gpu` 不可切分也不可超賣。** 這跟 CPU 完全不同 —— CPU 可以 request 0.5 核、可以超賣，GPU 不行。

### 6.3 三個選項，每個都有代價

| 選項 | 做法 | 代價 |
|---|---|---|
| **A. 接受停機** | `maxUnavailable: 1, maxSurge: 0`（或 `strategy: Recreate`） | 每次更新斷線 3–5 分鐘（vLLM 冷啟動） |
| **B. 多一張卡** | 加一個 GPU 節點 | 硬體成本。**on-prem 買不起就是買不起** |
| **C. time-slicing** | 一張卡對 k8s 宣告成 N 張 | 沒有記憶體與故障隔離（Part 7 會實測） |

先把 A 做一次，親眼看到停機。

**先開一個 bench pod**（叢集內的「客戶端」，Part 7、8、9 的壓測也都用它）：

```bash
# vLLM image 在 Part 4 已經被拉進 k3s 了，不用再下載；這個 pod 不要求 GPU
kubectl -n pricer run bench --image=vllm/vllm-openai:latest \
  --env="HF_TOKEN=$(cat ~/private/hf-token)" \
  --command -- sleep infinity
kubectl -n pricer wait --for=condition=Ready pod/bench --timeout=120s
```

改成選項 A 的策略，然後從 bench pod 每秒打一次 Service：

```bash
kubectl -n pricer patch deploy/model-server --type=json -p='[
  {"op":"replace","path":"/spec/strategy/rollingUpdate/maxSurge","value":0},
  {"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":1}
]'

# 終端 1：每秒打一次，印出時間戳與結果
kubectl -n pricer exec bench -- python3 -u -c "
import time, urllib.request
while True:
    try:
        code = urllib.request.urlopen('http://model-server:8080/health', timeout=2).status
    except Exception:
        code = 'DOWN'
    print(time.strftime('%H:%M:%S'), code)
    time.sleep(1)
"

# 終端 2：觸發更新
kubectl -n pricer rollout restart deploy/model-server
```

**停機時間 = 第一個 `DOWN` 到下一個 `200` 之間的秒數。** 把它記進 `RESULTS.md`。這是選項 A 的真實成本，也是說服老闆買第二張卡的數據。

**為什麼不從本機透過 port-forward 打**：`port-forward` 在建立時就綁定**某一個 pod**。舊 pod 一被刪，port-forward 就斷了，而且新 pod 起來後它**不會自動接上** —— 你會看到 `000` 一路延續下去，量到的是「port-forward 斷了多久」，不是服務停了多久。從 bench pod 打 Service 才會在新 pod Ready 的那一刻自動恢復。

> 用 Python 而不是 `curl`，是因為 vLLM image 一定有 Python，但不保證有 curl。

---

## Part 7：GPU time-slicing

**目的**：用 time-slicing 解開 Part 6 的死結，並把它的代價量出來。

### 7.1 做法

```bash
kubectl apply -f k8s/overlays/gpu/time-slicing-config.yaml

kubectl patch clusterpolicy/cluster-policy \
  -n gpu-operator --type merge \
  -p '{"spec":{"devicePlugin":{"config":{"name":"time-slicing-config","default":"any"}}}}'

# 等 device plugin 重啟，然後看節點容量
sleep 60
kubectl describe node | grep -A8 "Capacity:"
# nvidia.com/gpu: 3        ← 一張卡變成三張
kubectl get node -o jsonpath='{.items[0].metadata.labels}' | tr ',' '\n' | grep replicas
# nvidia.com/gpu.replicas=3
```

**現在必須把每個 pod 的 `--gpu-memory-utilization` 調小**（理由與數字見 7.2）。先用 1 個 replica，把 Part 6 的死結解開：

```bash
# 先確認 "0.85" 在 args 裡的索引 —— 照 overlay 的寫法是 12（11 是旗標名稱本身）
kubectl -n pricer get deploy model-server -o jsonpath='{.spec.template.spec.containers[0].args}' | tr ',' '\n' | cat -n

kubectl -n pricer patch deploy/model-server --type=json -p='[
  {"op":"replace","path":"/spec/template/spec/containers/0/args/12","value":"0.42"},
  {"op":"replace","path":"/spec/strategy/rollingUpdate/maxSurge","value":1},
  {"op":"replace","path":"/spec/strategy/rollingUpdate/maxUnavailable","value":0}
]'
kubectl -n pricer rollout status deploy/model-server
```

這次 rollout 不會卡住：新 pod 拿到第 2 個切片，新舊兩個 pod 各 0.42，加起來 0.84 塞得進一張卡。**⚠️ 索引寫錯（例如 11）會把 `--gpu-memory-utilization` 這個旗標本身換掉，vLLM 會因為參數錯誤起不來。**

再擴到 2 個 replica，量 7.3 的表：

```bash
kubectl -n pricer scale deploy/model-server --replicas=2
```

### 7.2 為什麼這個 workload 特別適合示範

**先算清楚塞得下幾個**（粗算，實際以啟動 log 為準）：

```
L4 顯存（nvidia-smi 顯示）        ≈ 22.5 GiB
每個 pod 能用的                    = 22.5 GiB × --gpu-memory-utilization
每個 pod 的固定開銷                ≈ bf16 權重 6 GiB + r=256 的 LoRA 槽位、activation、CUDA graph 約 1.5–2.5 GiB

2 pod × 0.42 → 每個 9.4 GiB，扣掉固定開銷還剩 1–2 GiB 給 KV cache   ✓
3 pod × 0.28 → 每個 6.3 GiB，連固定開銷都放不下                      ✗
```

**真正限制 replica 數的是「每個 pod 都要一份權重」，不是 KV cache。** KV cache 是按需分配的，150 token 的請求佔得很少，留一兩 GiB 就能跑很高的並發（見 Phase 2 Part 3）。

這也是量化在本 workload 的真正價值：FP8 把每份權重砍到約 3.6 GB，3 個 pod 才有可能（選作：把 overlay 換成 Phase 2 的 FP8 merged 權重）。

### 7.3 必須量的那張表

| 設定 | 吞吐 (req/s) | p50 TTFT | **p99 TTFT** | MAE |
|---|---|---|---|---|
| 1 pod 獨佔整張卡（0.85） | | | | |
| 2 pod time-sliced（各 0.42） | | | | |

**預期結果**：總吞吐略降（context switch 有成本），但 **p99 會明顯惡化**。

**這張表勝過任何架構圖。** 面試時它證明的是「我知道 time-slicing 的代價，而且我量過」。

### 7.4 四個必須能講的陷阱

1. **完全沒有記憶體隔離。** 兩個 pod 共用同一塊 24GB HBM，**k8s 對此一無所知**。兩個 pod 都用預設的 `--gpu-memory-utilization 0.9` → 第二個直接 CUDA OOM。這是最常見的翻車點。
2. **沒有故障隔離。** 一個 pod 觸發 XID error，同卡上所有 pod 一起死。
3. **`nvidia.com/gpu: 3` 是謊報。** scheduler 以為有三張卡，對記憶體壓力毫無感知。**這正是 production 要用 MIG 的理由。** 親眼看一次：2 個 replica 時再 `rollout restart`，第 3 個 pod 會**排程成功**（拿到第 3 個切片），然後在啟動時因為顯存不夠而 CrashLoop —— 不是 `Pending`。死結又回來了，只是這次卡在 k8s 看不到的地方。看完用 `kubectl -n pricer rollout undo deploy/model-server` 收回。
4. **效能非線性。** 見上面那張表。

### 7.5 隔離強度光譜（面試必考）

```
time-slicing   →   MPS            →   MIG              →   整卡獨佔
（無隔離）         （記憶體軟上限，     （硬體切分 SM        （完全隔離）
                    仍無錯誤隔離）       與記憶體）
```

| 場景 | 該選 |
|---|---|
| 多個小模型、流量低、可容忍互相影響 | time-slicing |
| SLA 敏感的多租戶 | **MIG** |
| 單一高吞吐服務 | 整卡獨佔 |

**⚠️ L4 是 Ada 架構，不支援 MIG**（只有 A100 / H100 / H200 那條線有）。所以本 lab 只能做 time-slicing，MIG 與 MPS 只能讀不能做 —— **誠實寫進 `KNOWN-GAPS.md`**。

---

## Part 8：DCGM —— 「GPU util 100%」是騙人的

**這一節的區辨度最高。** 面試官問「你怎麼知道 GPU 沒被浪費」，大部分人會說「看 nvidia-smi」，然後就沒了。

### 8.1 做法

GPU Operator 已經幫你裝好 DCGM exporter 了：

```bash
kubectl -n gpu-operator get pods | grep dcgm
kubectl -n gpu-operator port-forward svc/nvidia-dcgm-exporter 9400:9400 &

curl -s localhost:9400/metrics | grep -E "DCGM_FI_DEV_GPU_UTIL|DCGM_FI_PROF_SM_ACTIVE|DCGM_FI_PROF_PIPE_TENSOR_ACTIVE"
```

### 8.2 三個指標的差別

| 指標 | 它回答什麼 | 陷阱 |
|---|---|---|
| `DCGM_FI_DEV_GPU_UTIL` | 「**有沒有** kernel 在跑？」 | **只要有任何 kernel 在執行就是 100%**，即使它只用了 5% 的算力 |
| `DCGM_FI_PROF_SM_ACTIVE` | 「有多少比例的 SM 在工作？」 | 這才是真實的佔用率 |
| `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | 「Tensor Core 的使用率？」 | **對 LLM 最重要** —— 矩陣乘法都跑在這裡 |

### 8.3 實際觀察

壓測跑著的時候，同時看這三個數字：

```bash
watch -n 1 'curl -s localhost:9400/metrics | grep -E "^DCGM_FI_DEV_GPU_UTIL|^DCGM_FI_PROF_SM_ACTIVE|^DCGM_FI_PROF_PIPE_TENSOR_ACTIVE"'
```

**你很可能會看到**：`GPU_UTIL` 接近 100%，但 `SM_ACTIVE` 明顯更低，`PIPE_TENSOR_ACTIVE` 更低。

**把這三個數字記進 `RESULTS.md`。** 它們之間的差距就是「你以為的利用率」和「真實的利用率」之間的落差。

### 8.4 為什麼這對 on-prem 特別重要

**on-prem 的容量是固定的。** 你不能像雲端那樣「不夠就再開一台」，所以「現有的卡有沒有被榨乾」是個真金白銀的問題。

如果 `GPU_UTIL` 是 100% 但 `SM_ACTIVE` 只有 40%，代表：
- 可能是 batch 湊不起來（調 `--max-num-seqs`）
- 可能是被記憶體頻寬卡住（量化可能有幫助）
- 可能是 kernel launch overhead（檢查有沒有誤開 `--enforce-eager`）

**每一個都是可行動的結論。** 而只看 `nvidia-smi` 的話，你只會得到「100%，滿了，該買卡了」這個昂貴的錯誤結論。

### 8.5 順便：per-pod 的 GPU 歸屬

DCGM exporter 會在指標上打 pod 的標籤。這讓你能回答「哪個團隊用掉了多少 GPU 時間」——**成本歸屬的雛形**。

```promql
sum by (pod) (DCGM_FI_PROF_SM_ACTIVE)
```

在多租戶的 on-prem 平台上，這是計費與配額調整的依據。

---

## Part 9：可觀測性與 HPA 接上真指標

### 9.1 做法

裝跟 Phase 0 完全一樣的堆疊：

```bash
helm install monitoring prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  --set prometheus.prometheusSpec.retention=24h
```

ServiceMonitor 只要改一個字（vLLM 的 port 是 8000，metrics 路徑一樣）：

```bash
kubectl apply -f k8s/observability/servicemonitor.yaml
```

prometheus-adapter 的設定要換指標名：

```bash
sed -e 's/llamacpp:requests_deferred/vllm:num_requests_waiting/g' \
    -e 's/llamacpp:requests_processing/vllm:num_requests_running/g' \
    k8s/observability/prometheus-adapter-values.yaml > /tmp/adapter-gpu.yaml

helm install prometheus-adapter prometheus-community/prometheus-adapter \
  -n monitoring -f /tmp/adapter-gpu.yaml

kubectl apply -f k8s/observability/hpa.yaml
```

### 9.2 為什麼 HPA 的 YAML 一個字都不用改

因為 Phase 0 設計 prometheus-adapter 時，就把兩邊的指標**映射到同一個 k8s 指標名** `pricer_requests_waiting`：

```
llamacpp:requests_deferred   ─┐
                              ├─► pricer_requests_waiting ─► HPA
vllm:num_requests_waiting    ─┘
```

**這就是抽象層的價值。** HPA 不需要知道後端是 llama.cpp 還是 vLLM。

### 9.3 現在 HPA 才能真的擴容

Phase 0 的 HPA 因為單節點資源不夠，擴容行為是被扭曲的。現在有 time-slicing，HPA 可以真的從 1 擴到 2。先把上限壓到 2 —— 第 3 個 pod 顯存放不下（Part 7.2），擴上去只會 CrashLoop：

```bash
kubectl -n pricer patch hpa model-server -p '{"spec":{"maxReplicas":2}}'
```


```bash
# 壓測：用 Part 6 開的 bench pod（還沒開的話回去照 Part 6 開一個），直接打 Service
kubectl -n pricer exec bench -- vllm bench serve --backend openai \
  --base-url http://model-server:8080 \
  --model pricer --tokenizer meta-llama/Llama-3.2-3B \
  --dataset-name random \
  --random-input-len 150 --random-output-len 5 \
  --num-prompts 2000 --request-rate 100

# 觀察（另一個終端）
kubectl -n pricer get hpa model-server -w

# 做完收掉
kubectl -n pricer delete pod bench
```

**為什麼不從 port-forward 打**：`kubectl port-forward svc/...` 其實只會連到**其中一個** pod，不會分流。HPA 擴出來的新 pod 根本拿不到流量，你會看到「擴容了但延遲沒改善」這種假象。走 Service（`model-server:8080`，Service 的 port 是 8080，轉到容器的 8000）才會被 kube-proxy 分到所有 replica。

`--tokenizer` 為什麼必須寫，見 Phase 2 Part 2.1。

**用 Phase 2 的曲線回頭校正 HPA 門檻**：你在 Phase 2 找到「排隊長度到多少時 p95 開始爆掉」，那就是 `averageValue` 該設的數字。**這是把量測轉成設定的完整閉環。**

---

## Part 10：Helm 實務

這個 Phase 裝了四個 Helm chart（GPU Operator、kube-prometheus-stack、prometheus-adapter，Phase 0 還有 ingress-nginx），順手把這幾件事練起來：

```bash
helm list -A
helm get values gpu-operator -n gpu-operator        # 查目前實際生效的值
helm get values gpu-operator -n gpu-operator --all  # 含預設值
helm history gpu-operator -n gpu-operator
helm rollback gpu-operator 1 -n gpu-operator        # 回滾
```

### ⚠️ Helm 的三個實務陷阱

1. **CRD 不被 Helm 的升級管理。** `helm upgrade` **不會**更新 CRD（只有 `helm install` 會裝）。升級 GPU Operator 或 kube-prometheus-stack 時，CRD 要手動 apply。這是最常見的「升級後怪怪的」原因。
2. **`--set` 跟 `-f values.yaml` 的優先序**：`--set` 贏。混用時很容易搞不清楚實際生效的是什麼 —— 用 `helm get values` 確認。
3. **`helm uninstall` 不會刪 CRD，也不會刪 PVC。** 這是刻意的（避免誤刪資料），但會留下孤兒資源。teardown 時要手動檢查。

### Helm 還是 Kustomize？（高頻面試題）

本 lab 的分法：

| 用途 | 工具 | 為什麼 |
|---|---|---|
| 自己的應用 | **Kustomize** | 沒有模板語言，輸出可預測，`kubectl` 內建 |
| 第三方元件 | **Helm** | 有版本管理、依賴、rollback；而且人家只提供 chart |

**能講出「我按這個原則分」比選邊站有說服力。**

---

## Phase 3 驗收清單

- [ ] `kubectl get runtimeclass` 看得到 `nvidia`
- [ ] `kubectl describe node` 的 Capacity 有 `nvidia.com/gpu`
- [ ] `kubectl exec deploy/model-server -- nvidia-smi` 在 pod 裡看得到卡
- [ ] **MAE 跟 Phase 1 的 Docker 版一致**（證明 k8s 這層沒改變行為）
- [ ] 單卡下的 rollout 死結重現過，且量了選項 A 的停機時間
- [ ] time-slicing 生效，`nvidia.com/gpu: 3` 出現在節點容量
- [ ] **「1 pod vs 2 pod」的吞吐 / p99 / MAE 對照表完成**，並親眼看過第 3 個 pod 排程成功、卻因顯存不夠起不來
- [ ] **DCGM 的 `GPU_UTIL` vs `SM_ACTIVE` vs `PIPE_TENSOR_ACTIVE` 三個數字都記錄了**
- [ ] HPA 用 `vllm:num_requests_waiting` 實際擴容（`SuccessfulRescale`）
- [ ] HPA 門檻已用 Phase 2 的曲線校正過
- [ ] 所有數字進了 `RESULTS.md`

---

## 收工與完整 teardown

**這是最後一個要花錢的階段。做完之後務必清乾淨。**

```bash
# 1. 先把數據抓回本機（很重要，機器刪了就沒了）
scp -i <key> -r ubuntu@<IP>:~/bench-results ./bench-raw/

# 2. 停機（保留 EBS，之後想重來可以直接 start）
aws ec2 stop-instances --region us-east-1 --instance-ids <ID>

# 3. 確定不再需要了才 terminate
aws ec2 terminate-instances --region us-east-1 --instance-ids <ID>

# 4. ⚠️ 檢查孤兒資源（terminate 不一定會清乾淨）
aws ec2 describe-volumes --region us-east-1 \
  --filters "Name=status,Values=available" \
  --query 'Volumes[].[VolumeId,Size,CreateTime]' --output table

aws ec2 describe-addresses --region us-east-1 \
  --query 'Addresses[?AssociationId==null].[PublicIp,AllocationId]' --output table
```

**閒置的 EBS volume 和未綁定的 Elastic IP 會一直收費。** 這是「我以為我刪光了」帳單的頭號來源。

---

## 下一站

→ [`07-phase4-eks.md`](07-phase4-eks.md)：EKS。主體是**雲端↔地端對照表**（對 on-prem 職缺來說，講得出這張表比真的開一次 EKS 更有價值），實跑 runbook 放附錄。
