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
| 7. time-slicing | ✅ 必做 | 1 / 2 pod 的吞吐、p99、MAE 表 —— 整個 lab 最有說服力的一張表。7.4 第 3 點的 CrashLoop 示範 📖 讀就好 |
| 8. DCGM | ✅ 必做 | `GPU_UTIL` vs `SM_ACTIVE` vs `PIPE_TENSOR_ACTIVE`，區辨度最高。⚠️ `SM_ACTIVE` 預設沒開，要先做 8.1 的 ConfigMap |
| 9. 可觀測性 + HPA | ⏭️ 跳過 | 跟 Phase 0 重複（Prometheus、prometheus-adapter、HPA 都在 Phase 0 實跑過），又要再裝整套 stack，最花時間 |
| 10. Helm 實務 | 📖 讀就好 | 那幾個指令與陷阱讀過、能講就夠 |

**bench pod 在 Part 6 建立**，Part 7 的對照表和 Part 8 的 DCGM 觀察都用它產生壓測負載（指令見 Part 7.1 的 `kubectl exec bench -- vllm bench serve ...`；每次換一個這個 pod 沒用過的 `--seed`，避開 prefix cache）。做完 Part 8 就 `kubectl -n pricer delete pod bench`。

**驗收清單照做，但跳過 HPA 那兩項**（「HPA 實際擴容」、「HPA 門檻已校正」），並在 `KNOWN-GAPS.md` 誠實記一筆：「HPA 接 vLLM 指標只做到設計，未在 GPU 上實跑」。

---

## Part 1：裝 k3s 與 NVIDIA Container Toolkit

### 1.1 做法

```bash
# 在 Phase 1 那台 g6.xlarge 上
nvidia-smi          # DLAMI 已經有驅動了，先確認

# 1) NVIDIA Container Toolkit（讓容器看得到 GPU）
# 這顆 AMI 已預裝（Phase 1 的 docker run --gpus all 能跑就是靠它），先用下面兩個指令檢查，有了就跳過底下的安裝
nvidia-ctk --version
dpkg -l | grep nvidia-container-toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit

# 2) 才裝 k3s —— 順序很重要，見下方
curl -sfL https://get.k3s.io | sh -

sudo chmod 644 /etc/rancher/k3s/k3s.yaml
# 寫進 bashrc：之後另開的 SSH 視窗（壓測、觀察）才都連得到叢集
echo 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml' >> ~/.bashrc
source ~/.bashrc
kubectl get nodes

# 3) helm 要裝在這台機器上：Mac 上的 helm 連不到 k3s 的 API（安全群組只開 22）
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
helm version
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

# k3s 產生的 containerd 設定裡真的有 nvidia runtime 嗎（1.2 說的「先 toolkit 再 k3s」生效了沒）
sudo grep -n nvidia /var/lib/rancher/k3s/agent/etc/containerd/config.toml

# 直接在容器裡確認看得到 GPU
# 用 `k3s ctr` 而不是 `ctr`：這顆 AMI 有 Docker 自帶的 /usr/bin/ctr，連的是 Docker 的 containerd，
# k3s 安裝時因此不建 ctr 捷徑（2026-10-20 實測：`which -a ctr` 只有 /usr/bin/ctr）
sudo k3s ctr image pull docker.io/nvidia/cuda:12.4.0-base-ubuntu22.04
sudo k3s ctr run --rm --gpus 0 \
  docker.io/nvidia/cuda:12.4.0-base-ubuntu22.04 gputest nvidia-smi
```

> `ctr run --gpus` 是 ctr 自己呼叫 NVIDIA 的工具把卡塞進容器，**不經過** containerd 設定裡的 `nvidia` runtime（推論，未實測）。所以它證明的是「toolkit + 驅動在 k3s 的 containerd 底下可用」；「pod 走 `runtimeClassName: nvidia` 拿得到卡」要等 Part 4.3 的 `kubectl exec ... nvidia-smi` 才算數。上面的 `grep` 是兩者之間的那一步。

### ⚠️ 陷阱

- **這是「由下往上」排查順序的第二層。** 如果這一步失敗，不要往上去查 k8s —— 問題在 runtime 層。Phase 1 已經用 `docker run --gpus all` 驗過第一層了，所以你知道驅動是好的。
- **k3s 預設會裝 Traefik 當 ingress**。跟 Phase 0 用的 ingress-nginx 不同。要嘛接受（改 `ingressClassName: traefik`），要嘛 `--disable traefik` 再自己裝 nginx。**這是「在我機器上可以跑」的另一個來源** —— ingress annotation 是廠商特定的，nginx 的那些 `proxy-buffering` annotation 在 Traefik 上完全無效。**本 Phase 不用 Ingress**（壓測都是 bench pod 直接打 Service），保留預設的 Traefik 即可；不要為此裝 ingress-nginx —— 它已於 2026-03 退役（見 `docs/02` Part 7）。

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

先把 lab repo 弄上機器。repo 是 private、EC2 上也沒有 GitHub 憑證，所以不用 `git clone`，從**本機**推上去：

```bash
# 本機，repo 根目錄：本 Phase 在機器上只用到 k8s/（manifest、Helm values）和 src/（build pricer-api）
rsync -a -e "ssh -i private/pricer-lab.pem" k8s src ubuntu@<IP>:~/llm-inference-k8s-lab/
```

只推這兩個資料夾，不要推整個 repo：`models/`（Phase 0 的 GGUF，約 5.4GB）、`private/`（憑證）、`bench-raw/` 都是 gitignored 但在本機上，用 `--exclude` 列黑名單很容易漏。`eval/measure.py` 是在本機跑的（透過 SSH 通道），不用推。改了 manifest 之後重跑同一行就會同步。

之後都在 GPU 機器上：

```bash
cd ~/llm-inference-k8s-lab
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
  resources: { requests: { storage: 20Gi } }   # 必須跟 k8s/base/model-server.yaml 一致，見下方
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

**為什麼是 20Gi、而且要跟 base 一樣**：Part 4 的 `kubectl apply -k` 會套用 `k8s/base/model-server.yaml` 裡同名的 PVC（20Gi）。這裡建得比它大的話，等於要把 PVC 改小 —— k8s 不允許縮小 PVC，apply 會在這個物件上報錯（推論，未實測）。而且 k3s 預設的 `local-path` StorageClass **不會強制執行大小**：PVC 只是節點上的一個資料夾，真正的上限是 EBS 剩下的空間（Phase 1+2 跑完約剩 59G）。PVC 裡會放 adapter（1.5GB）和 HF cache（`HF_HOME=/models/hf`，bf16 權重約 6.4GB）。

**為什麼 HF cache 要放 PVC**：不然每次 pod 重啟都要重新從 HF 下載 6.4GB。在 k8s 裡 pod 重啟是家常便飯（滾動更新、OOM、節點維護），每次等 5 分鐘不可接受。

**PVC 跟 Phase 1/2 的 `~/hf-cache` 是什麼關係**：在 `local-path` 上，PVC 是 `/var/lib/rancher/k3s/storage/pvc-<uid>_pricer_model-weights/` 這個資料夾，跟 `~/hf-cache`、`~/adapter` 在**同一顆 EBS 上，但內容不共用**。所以 Part 4 第一次起 vLLM pod 時，會再從 HF 下載一次 6.4GB 到 PVC 裡（之後重啟就直接讀 PVC）。不必手動把 `~/hf-cache` 複製進去 —— 這次啟動剛好就是「k8s 裡含權重下載的冷啟動」，記下來給 `startupProbe` 當依據（Phase 1 docker 實測 249 秒）。

---

## Part 4：部署 gpu overlay

### 4.1 做法

```bash
# repo 已在 Part 3 用 rsync 推上機器
cd ~/llm-inference-k8s-lab

docker build -t pricer-api:dev src/pricer-api
# k3s 用自己的 image store（跟 Docker 的分開）。先存成檔案再匯入：
# `sudo ... <(docker save ...)` 不行 —— sudo 會關掉 <(...) 用的那個檔案描述子
docker save -o /tmp/pricer-api.tar pricer-api:dev
# ⚠️ 一定要 --all-platforms：這顆 AMI 的 Docker 29 用 containerd image store，docker save 出來是「index 包 index」，
# 不加的話 ctr 印 `saved` 但什麼都沒匯入，pricer-api 會 ImagePullBackOff（2026-10-20 實測）
sudo k3s ctr images import --all-platforms /tmp/pricer-api.tar && rm /tmp/pricer-api.tar
sudo k3s crictl images | grep pricer          # 要看得到才算匯入成功

# 先把 vLLM image 拉進 k3s（2026-10-20 實測 9GB、約 4 分 40 秒），下面量的冷啟動才不含下載
sudo k3s crictl pull docker.io/vllm/vllm-openai@sha256:c1c9f6fd5c109ba7f0546a59f5b2f15fb87f64c77782e90a27b648b42a8e67c3

kubectl kustomize k8s/overlays/gpu | less     # 先看再 apply
kubectl apply -k k8s/overlays/gpu

kubectl -n pricer get pods -w
```

**第一次會等很久，分兩段**：
1. 拉 vLLM image（10GB+）—— 上面已經用 `crictl pull` 先拉了。Phase 1 用 Docker 拉過的那份**不算數**，兩邊的 image store 分開。這段不計入 `startupProbe`。
2. `Running` 但還沒 `READY` —— 下載權重進 PVC（6.4GB）+ 編譯（k8s 裡沒有 `~/vllm-cache`）。這段就是 Part 3 說的「k8s 冷啟動」，記下 pod 建立到 Ready 的秒數；`startupProbe` 給了 900 秒（2026-10-20 實測 **230 秒**，Phase 1 docker 249 秒）：

```bash
kubectl -n pricer get pod -l app=model-server -o jsonpath='{.items[0].metadata.creationTimestamp}{"  →  "}{.items[0].status.conditions[?(@.type=="Ready")].lastTransitionTime}{"\n"}'
```

overlay 把 vLLM image 鎖在 Phase 1/2 量測用的 digest（vLLM 0.31.0，`k8s/overlays/gpu/kustomization.yaml`），跟前面的數字才可比。

### 4.2 base 跟 overlay 的差異一覽

**這是整個 lab 的關鍵驗證點：Phase 0 寫的東西到底沿用了多少。**

| 項目 | base（Phase 0 就寫好） | gpu overlay 加的 |
|---|---|---|
| 三種 probe 的結構 | ✅ | 只調 `failureThreshold`（90 vs 60） |
| `preStop` + grace period | ✅ | 不變 |
| `/dev/shm` 的 emptyDir | ✅ | 不變（Phase 0 就預留了） |
| securityContext（uid 10001、非 root） | ✅ | 不變，但要加 `HOME=/tmp`：vLLM image 的 HOME 是不可寫的 `/root`，編譯快取寫不進去 |
| container port 8080 | ✅ | 不變：vLLM 加 `--port 8080` 去聽它（預設 8000）。overlay 若另外宣告 8000，strategic merge 會留下 base 的 8080，兩個都叫 `http` 會撞名 |
| Service / ConfigMap / PVC | ✅ | 不變 |
| pricer-api 整個 | ✅ | 不變 |
| image 與 args | llama.cpp | **vLLM**（鎖 digest） |
| requests / limits | cpu 2、記憶體 6Gi / limit 8Gi | cpu 500m、4Gi / limit 12Gi：4 vCPU、約 15 GiB 的機器要排得下 Part 7 的 2～3 個 pod |
| `nvidia.com/gpu` | — | **新增** |
| `runtimeClassName: nvidia` | — | **新增** |
| toleration + nodeSelector | — | **新增** |
| HF_TOKEN 的 secretKeyRef | — | **新增** |

**大部分東西都沒變。** 這就是 Phase 0 花時間在零成本環境把骨架做對的回報。

### 4.3 驗證

```bash
# pod 真的拿到卡了嗎（排查順序的第四層）
kubectl -n pricer exec deploy/model-server -- nvidia-smi

kubectl -n pricer port-forward svc/model-server 8000:8080 &   # 機器上的 8000 → Service 的 8080（容器也是 8080）；本機的 SSH 通道 -L 8000:localhost:8000 接到這裡
curl -s localhost:8000/v1/models | python3 -m json.tool
```

**然後跑一次正確性驗證** —— 這是最重要的一步：

```bash
# 本機（透過 SSH 通道）
uv run --with datasets eval/measure.py \
  --base-url http://localhost:8000 --model pricer --size 200 \
  --label "phase3-k3s-vllm-bf16"
```

**MAE 應該跟 Phase 1 的 Docker 版一致（差距 <1%）。** 2026-10-20 實測 $40.79 vs Phase 1 的 $40.74，同設定再量一次是 $40.78 —— vLLM 在併發請求下每次湊成的批次不同，bf16 數值有微小差異，不會逐位元相同（Phase 0 的 llama.cpp 才是 4 次完全相同）。如果差很多（例如接近 $200），代表 k8s 這一層改變了什麼 —— 最常見的是請求打到了 `base` 而不是 `pricer`（pricer-api 的 `MODEL_NAME`、或 args 裡的 `--served-model-name` / `--lora-modules` 名稱對不上）。

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
# 用跟 model-server 同一個 digest（k8s/overlays/gpu/kustomization.yaml）：Part 4 已拉進 k3s，不用再下載；
# 寫 :latest 的話是另一個 image，會再拉 10GB+（下面 120 秒的 wait 會逾時），bench 工具版本也可能跟 server 不同
# 這個 pod 不要求 GPU
kubectl -n pricer run bench \
  --image=vllm/vllm-openai@sha256:c1c9f6fd5c109ba7f0546a59f5b2f15fb87f64c77782e90a27b648b42a8e67c3 \
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
# ⚠️ 6.1 那個卡住的 rollout 會在這一刻自己完成（先停舊 pod、再起新 pod）—— 這次的停機不量。
# 等它跑完再往下：
kubectl -n pricer rollout status deploy/model-server --timeout=15m

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

> **2026-10-20 實測：191 秒。** 新 pod 建立 → Ready 是 201 秒（比 docker 權重已快取的 121 秒多，因為 k8s 裡沒有編譯快取），但第一個 `DOWN` 比新 pod 建立晚 10 秒。推論（未驗證）：舊 pod 在 preStop 期間已是 terminating，但 Service 沒有其他 Ready 的 endpoint 時，kube-proxy 會繼續把流量送給 terminating 的 pod，直到程序真的停掉。

**為什麼不從本機透過 port-forward 打**：`port-forward` 在建立時就綁定**某一個 pod**。舊 pod 一被刪，port-forward 就斷了，而且新 pod 起來後它**不會自動接上** —— 你會看到 `000` 一路延續下去，量到的是「port-forward 斷了多久」，不是服務停了多久。從 bench pod 打 Service 才會在新 pod Ready 的那一刻自動恢復。

> 用 Python 而不是 `curl`，是因為 vLLM image 一定有 Python，但不保證有 curl。

---

## Part 7：GPU time-slicing

**目的**：用 time-slicing 解開 Part 6 的死結，並把它的代價量出來。

### 7.1 做法

**先量 7.3 表的第一列「1 pod 獨佔（0.85）」**—— 改成 0.42 之後就量不到了。從 bench pod 打 Service，熱身 seed 1、正式 seed 2（理由見 Phase 2 Part 1.2），只記第二次：

```bash
mkdir -p ~/bench-raw          # 壓測輸出都存這裡，收工時 scp 回本機
for s in 1 2; do
  kubectl -n pricer exec bench -- vllm bench serve --backend openai \
    --base-url http://model-server:8080 \
    --model pricer --tokenizer meta-llama/Llama-3.2-3B --dataset-name random \
    --random-input-len 150 --random-output-len 5 --num-prompts 500 --request-rate inf \
    --seed $s --percentile-metrics ttft,tpot,e2el --metric-percentiles 50,95,99 \
    2>/dev/null | tee ~/bench-raw/7-1pod-seed$s.txt
done
```

MAE 用 Part 4.3 的 `eval/measure.py`（label `phase3-k3s-1pod`）。⚠️ Part 4.3 開的 `port-forward` 綁的是當時那個 pod，Part 6 已經把它換掉了 —— 先關掉舊的再重開才量得到（之後每次 pod 被換掉都一樣）：

```bash
# 不能用 pkill -f "port-forward ..."：k3s 的 kubectl 是指向 k3s 的捷徑，會改寫自己的命令列，
# ps / pgrep 只看得到「kubectl」，pkill -f 比對不到（2026-10-20 實測）。改成按 port 殺：
fuser -k 8000/tcp; sleep 1
kubectl -n pricer port-forward svc/model-server 8000:8080 > /tmp/pf.log 2>&1 &
sleep 3; cat /tmp/pf.log      # 要看到 Forwarding from 127.0.0.1:8000
```

Phase 1 docker 版的參考值：35.77 req/s（多掛了 `pricer2 --max-loras 2`，不完全同條件）；2026-10-20 k3s 實測 34.54 req/s。

然後開 time-slicing：

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

**現在必須把每個 pod 的 `--gpu-memory-utilization` 調小**（理由與數字見 7.2）。

**這一次改 0.42 要用 `Recreate`（舊 pod 完全結束、顯存釋放後才起新的）**：舊 pod 還是 0.85（約 19 GiB），新 pod 只要跟它並存，就只看得到約 3 GiB 可用顯存，起不來。`maxSurge: 1 / maxUnavailable: 0` 會卡死；Part 6 的 `maxSurge: 0 / maxUnavailable: 1` 也不保險 —— RollingUpdate 不等舊 pod 結束，開了 time-slicing 後新 pod 馬上拿得到切片，可能在舊 pod 的 preStop／關機期間就啟動、先 crash 一次。`Recreate` 會等舊 pod 全部結束。

```bash
# 改顯存比例 + 這一次用 Recreate（會停機一次）
# 照 overlay 的寫法 "0.85" 在 args/12（11 是旗標名稱本身）。第一行 "test" 先核對，索引不對整個 patch 就被拒絕、什麼都不改
kubectl -n pricer patch deploy/model-server --type=json -p='[
  {"op":"test","path":"/spec/template/spec/containers/0/args/12","value":"0.85"},
  {"op":"replace","path":"/spec/template/spec/containers/0/args/12","value":"0.42"},
  {"op":"replace","path":"/spec/strategy","value":{"type":"Recreate"}}
]'
kubectl -n pricer rollout status deploy/model-server --timeout=15m

# 之後的 pod 都是 0.42，新舊並存塞得下（0.42 × 2 = 0.84）—— 改回零停機的滾動更新
kubectl -n pricer patch deploy/model-server --type=json -p='[
  {"op":"replace","path":"/spec/strategy","value":{"type":"RollingUpdate","rollingUpdate":{"maxSurge":1,"maxUnavailable":0}}}
]'
```

**⚠️ 索引寫錯（例如 11）會把 `--gpu-memory-utilization` 這個旗標本身換掉，vLLM 會因為參數錯誤起不來** —— 上面的 `test` 就是擋這個。

再擴到 2 個 replica，量 7.3 表的第二列。prefix cache 存在每個 vLLM 程序裡、pod 一換就沒了 —— 0.42 那次 rollout 已經換掉 pod，兩個 pod 的 cache 都是空的；照慣例仍換新的 seed 3 熱身、seed 4 正式：

```bash
kubectl -n pricer scale deploy/model-server --replicas=2
kubectl -n pricer rollout status deploy/model-server --timeout=15m
kubectl -n pricer logs -l app=model-server --tail=-1 | grep -E "KV cache size|Maximum concurrency"   # 7.2 的推算對不對

for s in 3 4; do
  kubectl -n pricer exec bench -- vllm bench serve --backend openai \
    --base-url http://model-server:8080 \
    --model pricer --tokenizer meta-llama/Llama-3.2-3B --dataset-name random \
    --random-input-len 150 --random-output-len 5 --num-prompts 500 --request-rate inf \
    --seed $s --percentile-metrics ttft,tpot,e2el --metric-percentiles 50,95,99 \
    2>/dev/null | tee ~/bench-raw/7-2pod-seed$s.txt
done
```

MAE 一樣用 `eval/measure.py`（label `phase3-k3s-2pod-timesliced`；pod 又換過了，先照上面用 `fuser` 重開 `port-forward`）。注意 `port-forward svc/...` 只會連到其中一個 pod（見 Part 9.3），兩個 pod 是同一份設定，MAE 量一個就夠。

**再量一組對照：1 pod、0.42。** 2 pod 那組同時改了兩件事（顯存切小、兩個 pod 輪流用 GPU），不加這組就分不出吞吐掉的是哪一個的代價。縮回 1 個時留下的那個 pod 可能還有 seed 3、4 的 prefix cache，換 seed 5、6：

```bash
kubectl -n pricer scale deploy/model-server --replicas=1
for s in 5 6; do
  kubectl -n pricer exec bench -- vllm bench serve --backend openai \
    --base-url http://model-server:8080 \
    --model pricer --tokenizer meta-llama/Llama-3.2-3B --dataset-name random \
    --random-input-len 150 --random-output-len 5 --num-prompts 500 --request-rate inf \
    --seed $s --percentile-metrics ttft,tpot,e2el --metric-percentiles 50,95,99 \
    2>/dev/null > ~/bench-raw/7-1pod-042-seed$s.txt
done
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

> ⚠️ **用 Phase 1/2 實測數字重算（2026-10-18，推算、未實測）**：上面的「1–2 GiB」可能太樂觀。Phase 1 ①（`--max-loras 2`）的 `Model loading took` 是 7.76 GiB，merged 是 6.04 GiB → 每個 r=256 LoRA 槽位約 0.86 GiB；本 overlay 只掛 1 個 adapter（預設 `--max-loras 1`），權重 + 槽位 ≈ 6.9 GiB。① 在 0.85 下的非 KV 開銷（activation、CUDA graph、CUDA context）≈ 19.1 − 7.76 − 9.07 ≈ 2.3 GiB。
>
> ```
> 2 pod × 0.42 → 9.45 − 6.9 − 2.3 ≈ 0.25 GiB 給 KV cache（約 2,000 token）
> ```
>
> 仍大於 `--max-model-len 512` 的啟動門檻，所以**應該起得來，但很緊**：並發上限變低，任何多一點的開銷（例如改成 `--max-loras 2`）就可能放不下。
>
> **2026-10-20 實測**：兩個 pod 都起來了。`Model loading took` 6.9 GiB ✅（跟推算一致）；`KV cache size` 各 **5,232 tokens**（約 0.57 GiB，並發上限 10.2）—— 推算的約 2,000 太悲觀，非 KV 開銷在 0.42 下比 0.85 時小。`nvidia-smi` 每個程序 9,068 MiB。

> ⚠️ **單 pod 容量參考**（Phase 2，2026-10-18，docker 單獨跑、runtime LoRA、0.85）：飽和約 36 req/s，p99 TTFT 轉折點約 30 req/s。time-sliced 的兩個 pod 共用同一張卡的算力（compute-bound），**總和**預期不會超過這個數。

**真正限制 replica 數的是「每個 pod 都要一份權重」，不是 KV cache。** KV cache 是按需分配的，150 token 的請求佔得很少，留一兩 GiB 就能跑很高的並發（見 Phase 2 Part 3）。

這也是量化在本 workload 的真正價值：FP8 把每份權重砍到約 3.6 GB，3 個 pod 才有可能（選作：把 overlay 換成 Phase 2 的 FP8 merged 權重）。

### 7.3 必須量的那張表

| 設定 | 吞吐 (req/s) | p50 TTFT | **p99 TTFT** | MAE |
|---|---|---|---|---|
| 1 pod 獨佔整張卡（0.85） | | | | |
| 1 pod（0.42，對照組） | | | | — |
| 2 pod time-sliced（各 0.42） | | | | |

**預期結果**：總吞吐略降（context switch 有成本），但 **p99 會明顯惡化**。

> **2026-10-20 實測（數字見 `RESULTS.md`）**：降得不只「略」—— 34.54 → 28.13 req/s（−19%），p99 TTFT +23%（`--request-rate inf` 下 p99 TTFT 約等於整批跑完的時間，主要跟著吞吐走）。對照組拆出來：顯存切成 0.42 先掉 10.7%，兩個 pod 輪流用 GPU 再掉 8.8%。MAE 不受影響（$40.53）。

**這張表勝過任何架構圖。** 面試時它證明的是「我知道 time-slicing 的代價，而且我量過」。

### 7.4 四個必須能講的陷阱

1. **完全沒有記憶體隔離。** 兩個 pod 共用同一塊 24GB HBM，**k8s 對此一無所知**。兩個 pod 都用預設的 `--gpu-memory-utilization 0.9` → 第二個直接 CUDA OOM。這是最常見的翻車點。
2. **沒有故障隔離。** XID 是 NVIDIA 驅動回報的錯誤代碼（`sudo dmesg | grep -i xid`）。硬體或驅動層級的 XID（例如 48 ECC 錯誤、79 GPU 掉出 PCIe 匯流排）要重置整張卡，同卡上所有 pod 一起掛；應用層的錯誤（例如 13、31 記憶體存取違規）通常只終止出錯的那個程序，但 time-slicing 不保證其他 pod 沒事。
3. **`nvidia.com/gpu: 3` 是謊報。** scheduler 以為有三張卡，對記憶體壓力毫無感知。**這正是 production 要用 MIG 的理由。** （選作，精簡路線讀就好）親眼看一次：2 個 replica 時再 `rollout restart`，第 3 個 pod 會**排程成功**（拿到第 3 個切片），然後在啟動時因為顯存不夠而 CrashLoop —— 不是 `Pending`。死結又回來了，只是這次卡在 k8s 看不到的地方。看完用 `kubectl -n pricer rollout undo deploy/model-server` 收回。
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

GPU Operator 已經幫你裝好 DCGM exporter 了，**但 `SM_ACTIVE` 預設不會輸出**。

GPU Operator 讓 exporter 讀 image 內建的 `/etc/dcgm-exporter/dcp-metrics-included.csv`，那份清單裡 `DCGM_FI_DEV_GPU_UTIL` 和 `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` 是開的，**`DCGM_FI_PROF_SM_ACTIVE` 被註解掉了**（2026-10 查 NVIDIA/gpu-operator main 的 `assets/state-dcgm-exporter/0800_daemonset.yaml` 與 NVIDIA/dcgm-exporter `4.8.4` 的 csv）。不處理的話 `grep SM_ACTIVE` 什麼都抓不到，也不會有任何錯誤。

**先換一份自訂清單**：

```bash
# image 是 distroless 的，沒有 cat，不能 exec 進去拿 —— 從 GitHub 抓「同一版」的清單。
# image tag 長這樣：4.6.1-4.8.4-distroless（<DCGM>-<exporter>-<變體>）。
# GitHub 的 git tag 大多是 <DCGM>-<exporter>（例如 4.6.0-4.8.3），但也有只寫 exporter 版本的（例如 4.8.4），兩個都試。
TAG=$(kubectl -n gpu-operator get ds nvidia-dcgm-exporter \
  -o jsonpath='{.spec.template.spec.containers[0].image}' | sed -E 's/.*://; s/-(distroless|ubuntu.*)$//')
echo "$TAG"                                   # 例如 4.6.1-4.8.4
rm -f /tmp/dcgm-orig.csv
for ref in "$TAG" "${TAG##*-}"; do
  curl -fsSL "https://raw.githubusercontent.com/NVIDIA/dcgm-exporter/${ref}/etc/dcp-metrics-included.csv" \
    -o /tmp/dcgm-orig.csv && { echo "用 $ref"; break; }
done
test -s /tmp/dcgm-orig.csv || echo "❌ 兩個 ref 都抓不到，去 https://github.com/NVIDIA/dcgm-exporter/tags 找對應的 tag"

sed 's/^# *DCGM_FI_PROF_SM_ACTIVE/DCGM_FI_PROF_SM_ACTIVE/' /tmp/dcgm-orig.csv > /tmp/dcgm-metrics.csv
grep -E "^DCGM_FI_(DEV_GPU_UTIL|PROF_SM_ACTIVE|PROF_PIPE_TENSOR_ACTIVE)" /tmp/dcgm-metrics.csv   # 三行都要在，少一行就別往下做

# key 必須叫 dcgm-metrics.csv（GPU Operator 的規定）
kubectl -n gpu-operator create configmap custom-dcgm-metrics --from-file=dcgm-metrics.csv=/tmp/dcgm-metrics.csv
kubectl patch clusterpolicy/cluster-policy --type merge \
  -p '{"spec":{"dcgmExporter":{"config":{"name":"custom-dcgm-metrics"}}}}'

# 等 exporter 重建。operator 是非同步改 DaemonSet 的：先等 DaemonSet 真的引用了新 ConfigMap，
# 否則 rollout status 可能回報的是「舊的那版已完成」
timeout 300 bash -c 'until kubectl -n gpu-operator get ds nvidia-dcgm-exporter -o yaml | grep -q custom-dcgm-metrics; do sleep 5; done'
kubectl -n gpu-operator rollout status ds/nvidia-dcgm-exporter
```

> DaemonSet 名稱依 GPU Operator 版本可能不同，對不上就 `kubectl -n gpu-operator get ds | grep dcgm` 看實際名字。L4 支不支援 profiling（`PROF_*`）指標要在機器上確認：下一步 `curl` 若連 `PIPE_TENSOR_ACTIVE` 都沒有，就是硬體或驅動不支援，記進 `KNOWN-GAPS.md`。
>
> 2026-10-20 實測（GPU Operator v26.7.2，image `4.6.1-4.8.4`）：git tag `4.6.1-4.8.4` 是 404，`4.8.4` 抓得到；原清單 `SM_ACTIVE` 確實被註解掉；L4 三個指標都有值。

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

**⚠️ exporter 約每 30 秒才更新一次，`PROF_*` 是那 30 秒的平均**（2026-10-20 實測：數值在 05:10:59、05:11:30、05:11:58 才跳）。500 筆的壓測只跑約 15 秒、`vllm bench serve` 自己啟動又要十幾秒，取樣 25 秒會全是 0。所以壓測放大到 2000 筆（約 65 秒）、取樣 90 秒、附時間戳，旁邊用 `nvidia-smi` 當對照；**只採用整段 30 秒都落在壓測期間的那一筆**。換一個這個 pod 沒用過的 seed：

```bash
M='^DCGM_FI_(DEV_GPU_UTIL|PROF_SM_ACTIVE|PROF_PIPE_TENSOR_ACTIVE)'
kubectl -n pricer exec bench -- vllm bench serve --backend openai \
  --base-url http://model-server:8080 \
  --model pricer --tokenizer meta-llama/Llama-3.2-3B --dataset-name random \
  --random-input-len 150 --random-output-len 5 --num-prompts 2000 --request-rate inf \
  --seed 8 2>/dev/null | grep "Request throughput" &
for i in $(seq 45); do
  echo "$(date +%T) $(curl -s localhost:9400/metrics | grep -E "$M" | sed -E 's/\{.*\}//; s/DCGM_FI_(DEV_|PROF_)//' | tr '\n' ' ') smi=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits)"
  sleep 2
done | tee ~/bench-raw/8-dcgm.txt
```

**原本預期**：`GPU_UTIL` 接近 100%，但 `SM_ACTIVE` 明顯更低，`PIPE_TENSOR_ACTIVE` 更低。

**2026-10-20 實測（1 pod、0.42）推翻了一半**：`GPU_UTIL` 99%、`SM_ACTIVE` **0.95**、`PIPE_TENSOR_ACTIVE` **0.52**。SM 並沒有閒著（compute-bound，跟 Phase 2 一致）；落差在 Tensor Core —— 只有一半時間在做矩陣乘法，其餘在跑 attention、逐元素運算、取樣與資料搬移。

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

ServiceMonitor 不用改（vLLM 在 overlay 裡也聽 8080、port 名字一樣叫 `http`，metrics 路徑一樣）：

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

**為什麼不從 port-forward 打**：`kubectl port-forward svc/...` 其實只會連到**其中一個** pod，不會分流。HPA 擴出來的新 pod 根本拿不到流量，你會看到「擴容了但延遲沒改善」這種假象。走 Service（`model-server:8080`，Service 和容器都是 8080）才會被 kube-proxy 分到所有 replica。

`--tokenizer` 為什麼必須寫，見 Phase 2 Part 2.1。

**用 Phase 2 的曲線回頭校正 HPA 門檻**：你在 Phase 2 找到「排隊長度到多少時 p95 開始爆掉」，那就是 `averageValue` 該設的數字。**這是把量測轉成設定的完整閉環。**

> ⚠️ **Phase 2 精簡路線沒量到排隊長度**（2026-10-18）：request-rate 掃描只記了 TTFT，轉折點約 **30 req/s**（p99 495 ms → 35 req/s 時 910 ms），但沒有同時記 `vllm:num_requests_waiting`。所以 `k8s/observability/hpa.yaml` 的 `averageValue: "2"` 目前**沒有實測依據**。補量方法：在 Phase 3 的 1 pod 上用 Phase 2 Part 2.3 的掃描指令跑 25 / 30 / 35 req/s，同時 `watch` `num_requests_waiting`（Phase 2 Part 7.1 視窗 2），取 30 req/s 時的穩態排隊長度當 `averageValue`。
>
> 另外，上面的壓測指令沒指定 `--seed`（預設 0）：如果同一個 pod 之前已經跑過 seed 0，prefix cache 會讓排隊變短、HPA 不容易觸發。加上一個沒用過的 `--seed`（例如 `--seed 900`）。

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

2026-10-20 精簡路線實跑結果（未勾的是精簡路線刻意跳過的，見 `KNOWN-GAPS.md`）：

- [x] `kubectl get runtimeclass` 看得到 `nvidia`
- [x] `kubectl describe node` 的 Capacity 有 `nvidia.com/gpu`
- [x] `kubectl exec deploy/model-server -- nvidia-smi` 在 pod 裡看得到卡
- [x] **MAE 跟 Phase 1 的 Docker 版一致**（證明 k8s 這層沒改變行為）—— $40.79 vs $40.74
- [x] 單卡下的 rollout 死結重現過，且量了選項 A 的停機時間 —— 191 秒
- [x] time-slicing 生效，`nvidia.com/gpu: 3` 出現在節點容量
- [x] **「1 pod vs 2 pod」的吞吐 / p99 / MAE 對照表完成**（另加 1 pod 0.42 對照組）；第 3 個 pod CrashLoop 示範精簡路線未做（📖 讀就好）
- [x] **DCGM 的 `GPU_UTIL` vs `SM_ACTIVE` vs `PIPE_TENSOR_ACTIVE` 三個數字都記錄了** —— 99% / 0.95 / 0.52
- [ ] HPA 用 `vllm:num_requests_waiting` 實際擴容（`SuccessfulRescale`）—— 精簡路線跳過
- [ ] HPA 門檻已用 Phase 2 的曲線校正過 —— 精簡路線跳過
- [x] 所有數字進了 `RESULTS.md`

---

## 收工與完整 teardown

**這是最後一個要花錢的階段。做完之後務必清乾淨。**

```bash
# 1. 先把數據抓回本機（很重要，機器刪了就沒了）：本 Phase 的壓測輸出都 tee 在 ~/bench-raw（Part 7.1）
scp -i private/pricer-lab.pem -r ubuntu@<IP>:~/bench-raw ./

# 2. 停機（保留 EBS，之後想重來可以直接 start）
aws ec2 stop-instances --region us-east-1 --instance-ids <ID>

# 3. 確定不再需要了才 terminate
aws ec2 terminate-instances --region us-east-1 --instance-ids <ID>

# 4. ⚠️ 檢查孤兒資源（terminate 不一定會清乾淨）：六個區塊全是「（無）」、結束碼 0 才算清乾淨
scripts/aws-check.sh
```

**閒置的 EBS volume 和未綁定的 Elastic IP 會一直收費。** 這是「我以為我刪光了」帳單的頭號來源。

---

## 下一站

→ [`07-phase4-eks.md`](07-phase4-eks.md)：EKS。主體是**雲端↔地端對照表**（對 on-prem 職缺來說，講得出這張表比真的開一次 EKS 更有價值），實跑 runbook 放附錄。
