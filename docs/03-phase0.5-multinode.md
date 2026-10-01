# LLM Inference on Kubernetes 實驗室 — 03：Phase 0.5，kind 三節點

> 成本：**$0**。純 CPU，不需要 GPU，不需要 AWS。
> 產出：單節點練不到的那一半 k8s，以及一份「我知道 RWO 在真實叢集會怎樣」的實證。
> 為什麼有這個 Phase：**Phase 0 的單節點環境會給你錯誤的直覺**，這裡負責矯正。

---

## 這一階段的目的

Phase 0 用 OrbStack 很方便，但方便是有代價的 —— **有三件事你以為學會了，其實沒有**：

| 你在單節點看到的 | 真實叢集的樣子 |
|---|---|
| `ReadWriteOnce` 的 PVC 可以被多個 pod 同時掛載 | 另一個節點上的 pod 起不來（kind 上是 `volume node affinity conflict`，EBS 類是 `Multi-Attach error`） |
| LoadBalancer 開箱就能用 | 卡在 `Pending`，on-prem 要自己裝 MetalLB 並管 IP 池 |
| `drain` 不知道會發生什麼（唯一的節點） | 節點維護是 on-prem 的日常，而且對 LLM 特別痛 |

**還有三件事單節點根本做不到**：真正的排程決策（沒有選擇）、`topologySpreadConstraints` 的正向行為、以及 on-prem 的資源治理（配額、優先級、搶佔）。

這一個 Phase 完全服務「k8s 最重要」這個目標，而且**一毛錢都不用花**。

**十個部分：**

1. 開三節點叢集
2. 刻意重現 RWO 的跨節點失敗
3. RWX 與模型分發的真實取捨
4. MetalLB —— on-prem 的 LoadBalancer
5. 排程：taint、toleration、affinity、spread
6. cordon / drain / PDB 與那個真實的兩難
7. 資源治理：ResourceQuota 與 LimitRange
8. 用假的 extended resource 零成本練 GPU 排程
9. PriorityClass 與 preemption
10. Job / CronJob 做離線評估

---

## Part 1：開三節點叢集

### 1.1 做法

```bash
cd ~/Desktop/side-projects/llm-inference-k8s-lab

# ⚠️ 先把 OrbStack VM 的記憶體從 8 GiB 調到 12 GiB（理由見下方陷阱）
orb config get memory_mib                 # 記下原本的值，收工時調回去
orb config set memory_mib 12288
orb stop && orb start                     # 記憶體設定要重啟 OrbStack 才生效

orb stop k8s                              # ⚠️ 先關掉 OrbStack 的 k8s
kind create cluster --config k8s/kind/cluster.yaml
kubectl config use-context kind-pricer-lab
kubectl get nodes -L node-role.pricer/gpu
```

你應該看到三個節點，其中一個 worker 有 `node-role.pricer/gpu=true` 標籤。

把 Phase 0 的東西搬過來：

```bash
# kind 不像 OrbStack 那樣直接吃本機 image，要手動載入
docker build -t pricer-api:dev src/pricer-api
kind load docker-image pricer-api:dev --name pricer-lab

# kind 是全新的叢集，PVC 是空的 —— LoRA 要重新放一次（做法同 Phase 0 Part 3）。
# 不放的話 model-server 的 initContainer 會故意失敗（找不到 adapter 就不准啟動）。
kubectl apply -f k8s/base/namespace.yaml
kubectl -n pricer apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: model-weights }
spec:
  accessModes: ["ReadWriteOnce"]
  resources: { requests: { storage: 20Gi } }
EOF
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

kubectl apply -k k8s/overlays/local
kubectl -n pricer get pods -w      # model-server 第一次啟動會下載 3.4GB 的 base model
```

> 這裡先手動建的 PVC 跟 base 裡的定義一模一樣，所以接下來的 `apply -k` 不會衝突。
> kind 的預設 StorageClass 是 `WaitForFirstConsumer`：PV 會建在 `loader` 被排到的那個節點上，之後 model-server 也會被綁到那個節點 —— 這正是 Part 2 要觀察的東西。

### 1.2 為什麼是 kind 而不是別的

| | kind | minikube（多節點） | k3d |
|---|---|---|---|
| 節點形式 | 每節點一個容器 | 每節點一個容器/VM | 每節點一個容器 |
| 記憶體開銷 | 最小 | 較大 | 小 |
| 換 CNI | `disableDefaultCNI` 一行 | 可以但較繞 | 可以 |
| 普及度 | k8s 官方 SIG 維護，CI 標配 | 高 | 中 |

**kind 的關鍵優勢是「可以換 CNI」** —— 這對練 NetworkPolicy 是必要的（見 Part 11）。

### ⚠️ 陷阱

- **記憶體的上限是 OrbStack VM，不是 Mac 的 16GB。** kind 的三個「節點」都是跑在 OrbStack VM 裡的容器，**共用同一份 VM 記憶體**（預設 8 GiB）。用 Phase 0 的實測數字算：

  | | 記憶體 |
  |---|---|
  | 一個 llama-server（Q8_0 + LoRA，`--cache-ram 0`） | 穩定 4.3 GiB，高峰 4.66 GiB |
  | kind 三節點空載 | 約 1.5–2 GiB |
  | 一個 model-server + 叢集 | 約 6.5 GiB —— 8 GiB 塞得下 |
  | **兩個 model-server + 叢集** | **約 11 GiB —— 8 GiB 塞不下** |

  Part 3（RWX，兩個 replica 分散在兩個節點）和 Part 5 / 8 / 9（改 pod template 觸發 rollout，新舊 pod 短暫並存）都會同時跑兩個 llama-server，所以要調到 12 GiB。代價是 macOS 只剩約 4GB，做這個 Phase 時先關掉瀏覽器等吃記憶體的程式。
- **scheduler 看到的記憶體是假的。** kind 節點容器沒有記憶體上限，所以**每個節點的 kubelet 都把整台 VM 的記憶體當成自己的 allocatable**。scheduler 以為兩個 worker 各有 12 GiB，會很放心地一邊放一個 model-server —— 實際上它們搶的是同一份 12 GiB。超過時不是 pod 被 `OOMKilled`，而是整台 VM **System OOM**，被砍的可能是任何行程（見 `POSTMORTEMS.md` 事故 1 最後一段）。真實叢集的節點是獨立機器，不會有這個問題 —— **這是 kind 的限制，不是 k8s 的行為**。
- **不要同時開 OrbStack k8s 和 kind。** 兩個叢集搶同一台 VM。切換時 `orb stop k8s`。
- **image 要手動 load。** `kind load docker-image` 這一步 Phase 0 沒有（OrbStack 直接共用 Docker 的 image store）。忘了做的話 pod 會卡在 `ErrImageNeverPull`。
- **kind 的預設 CNI（kindnet）不強制執行 NetworkPolicy。** 跟 OrbStack 一樣的陷阱。要練就得換 Calico。
- **kind 叢集不會自動保存。** 重開機後 `kind get clusters` 還在，但容器停了，`docker start` 那幾個容器就能恢復。

---

## Part 2：刻意重現 RWO 的跨節點失敗

**目的**：矯正 Phase 0 留下的最危險的錯誤直覺。

### 2.1 做法

```bash
# 把 model-server 擴到 2 個 replica，並強迫它們分散到不同節點
kubectl -n pricer patch deploy/model-server --type=json -p='[
  {"op":"replace","path":"/spec/replicas","value":2},
  {"op":"add","path":"/spec/template/spec/affinity","value":{
    "podAntiAffinity":{
      "requiredDuringSchedulingIgnoredDuringExecution":[{
        "labelSelector":{"matchLabels":{"app":"model-server"}},
        "topologyKey":"kubernetes.io/hostname"
      }]
    }
  }}
]'

kubectl -n pricer get pods -l app=model-server -o wide -w
```

### 2.2 你會看到什麼

**至少有一個 model-server pod 起不來。** 卡在哪一步、錯誤長什麼樣，取決於 PV 背後是哪種儲存：

```bash
kubectl -n pricer get pods -l app=model-server -o wide
kubectl -n pricer describe pod -l app=model-server | grep -A5 "FailedScheduling\|FailedAttachVolume"
```

**在 kind 上（預設的 local-path provisioner）—— 預期是這個**：pod 卡在 `Pending`

```
Warning  FailedScheduling  0/3 nodes are available:
  1 node(s) didn't match pod anti-affinity rules,
  1 node(s) had untolerated taint {node-role.kubernetes.io/control-plane: },
  1 node(s) had volume node affinity conflict.
```

**在 EBS、Ceph RBD 這類網路區塊儲存上**：pod 排上了，但卡在 `ContainerCreating`

```
Warning  FailedAttachVolume  Multi-Attach error for volume "pvc-xxxx"
                             Volume is already used by pod(s) model-server-xxx
```

> ⚠️ 上面 kind 那段是依 local-path 的機制推論的預期輸出，確切措辭依 k8s 版本而異。**以你實際看到的為準，貼進 `RESULTS.md`。**

**兩種都是同一個道理（RWO 綁節點），只是擋下來的時間點不同**：

| | kind 的 local-path | EBS / Ceph RBD |
|---|---|---|
| PV 本質 | 某個節點上的一個目錄（hostPath） | 一顆可以拔下來換節點掛的網路磁碟 |
| PV 上有沒有 `nodeAffinity` | **有**，寫死在建立它的那個節點 | 沒有（可以跟著 pod 走） |
| 誰擋下來 | **scheduler**，排程時就知道去別的節點沒用 → `Pending` | **attach/detach controller**，pod 排上了才發現磁碟還掛在別的節點 → `ContainerCreating` |

`kubectl get pv -o yaml | grep -A8 nodeAffinity` 可以親眼看到 local-path 的 PV 被綁在哪個節點。

**順帶一提**：這個 patch 改了 pod template，會觸發 rollout。新 pod 除了要避開舊 pod（anti-affinity），又被 PV 綁回舊 pod 所在的節點 —— 兩個條件互相矛盾，rollout 會一直卡著，舊 pod 繼續跑。所以這一步實際上只會有**一個** llama-server 在跑。

### 2.3 為什麼 Phase 0 看不到這個

**因為所有 pod 都在同一個節點上。** `ReadWriteOnce` 的語意是「同一時間只能被**一個節點**掛載」—— 注意是節點，不是 pod。同節點上的多個 pod 共掛 RWO 完全合法。

單節點環境永遠滿足這個條件，所以你會建立「RWO 可以多 pod 共享」這個**完全錯誤**的直覺，然後在真實叢集上被打臉。

| 存取模式 | 全名 | 語意 |
|---|---|---|
| **RWO** | ReadWriteOnce | 一個**節點**可讀寫（該節點上多個 pod 都能用） |
| **RWOP** | ReadWriteOncePod | 一個 **pod** 可讀寫（k8s 1.22+，真正的獨佔） |
| **ROX** | ReadOnlyMany | 多個節點唯讀 |
| **RWX** | ReadWriteMany | 多個節點可讀寫（需要 NFS / CephFS 之類的後端） |

**對 LLM 服務的意義**：模型權重是**唯讀**的。所以正解不是硬要 RWX，而是要嘛 **ROX**，要嘛乾脆每個節點各存一份（見 Part 3）。

### ⚠️ 陷阱

**這個失敗在單節點上永遠不會出現，所以它是「在我機器上可以跑」的經典案例。** 面試時被問「你怎麼確認你的 manifest 在真實叢集會動」，這就是你的答案。

**先別還原**，Part 3 要繼續用這個壞掉的狀態。

---

## Part 3：RWX 與模型分發的真實取捨

**目的**：解決 Part 2 的問題，順便面對 on-prem 最常被問的架構題。

### 3.1 做法：裝一個叢集內的 NFS

```bash
helm repo add csi-driver-nfs https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/master/charts
helm repo update

# 先要有一台 NFS server（實驗用，正式環境是外部儲存設備）
kubectl apply -f https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/master/deploy/example/nfs-provisioner/nfs-server.yaml

helm install csi-driver-nfs csi-driver-nfs/csi-driver-nfs \
  --namespace kube-system

kubectl apply -f - <<'EOF'
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: nfs-rwx
provisioner: nfs.csi.k8s.io
parameters:
  server: nfs-server.default.svc.cluster.local
  share: /
reclaimPolicy: Delete
volumeBindingMode: Immediate
EOF
```

改用 RWX 的 PVC。**只換 PVC，不動 Deployment** —— Part 2 加的 `replicas: 2` 與 anti-affinity 要留著：

```bash
# 1) 先把 model-server 縮到 0，讓舊 PVC 沒人在用（有 pod 掛著的 PVC 刪不掉，會卡在 Terminating）
kubectl -n pricer scale deploy/model-server --replicas=0
kubectl -n pricer wait --for=delete pod -l app=model-server --timeout=120s

# 2) 換成同名的 RWX PVC
kubectl -n pricer delete pvc model-weights
kubectl -n pricer apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: model-weights }
spec:
  accessModes: ["ReadWriteMany"]
  storageClassName: nfs-rwx
  resources: { requests: { storage: 20Gi } }
EOF

# 3) 把模型放進新的 PVC —— 這次 base model 也一起放（理由見下）
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
kubectl -n pricer cp models/base.gguf loader:/models/base.gguf
kubectl -n pricer cp models/pricer-lora.gguf loader:/models/pricer-lora.gguf
kubectl -n pricer exec loader -- ls -lh /models
kubectl -n pricer delete pod loader

# 4) 擴回 2 個 replica
kubectl -n pricer scale deploy/model-server --replicas=2
kubectl -n pricer get pods -l app=model-server -o wide -w
```

這次兩個 replica 會分別排到兩個節點上，而且**都能掛載成功**。（這一步會同時跑兩個 llama-server，約 11 GiB —— Part 1 把 VM 調到 12 GiB 就是為了這裡。）

**為什麼不「刪掉 Deployment 再 `kubectl apply -k`」**：
- `apply -k` 會把 Deployment 還原成 base 的樣子：`replicas: 1`、沒有 anti-affinity。你就看不到「兩個 replica 分散在兩個節點」了。
- base 裡的 PVC 寫的是 `ReadWriteOnce`，叢集裡現在的是 `ReadWriteMany`。**PVC 的 spec 建立後不可變更**，apply 會直接報錯（`spec is immutable`）。

**為什麼 base model 也用 `kubectl cp` 放進去**：initContainer 看到 `/models/base.gguf` 已經存在就會跳過下載，省掉再抓一次 3.4GB。另外 model-server 是以非 root（uid 10001）執行的，NFS 上的目錄不一定開放給非 root 寫入；由 `loader`（root）先放好檔案，model-server 就只需要讀。

### ⚠️ 陷阱

**從這裡開始，這個 Phase 不要再 `kubectl apply -k k8s/overlays/local`。** 理由同上：PVC 已經換成 RWX，apply 會因為 spec 不可變而報錯；就算報錯前其他資源套用成功了，Deployment 也會被還原，把你後面 patch 上去的設定（toleration、nodeSelector、假 GPU……）全部洗掉。後面的 Part 都用 `kubectl patch` 改，就是這個原因。

### 3.2 為什麼這不是「正解」，只是「一個解」

**這是 on-prem 面試幾乎必問的題目**：「模型 200GB，5 個 GPU 節點，怎麼分發？」

四種做法，沒有一個是全面最佳：

| 做法 | 冷啟動 | 空間 | 可靠性 | 適用 |
|---|---|---|---|---|
| **烤進 image** | 慢（pull 數百 GB） | 每節點一份 | 最高（不可變、air-gap 友善） | 模型小、更新極少 |
| **RWX（NFS / CephFS）** | 慢（每次都走網路讀） | **只存一份** | NFS 是單點故障，也是頻寬瓶頸 | 節點多、啟動時間不敏感 |
| **每節點 local PV + 預先 rsync** | **最快**（本地 NVMe） | 每節點一份 | 要自己管一致性與回收 | 追求最低冷啟動 |
| **initContainer 從內部 MinIO/S3 拉到 local PV** | 中（每次冷啟動拉一次） | 每節點一份 | 有版本控制 | **最常見的生產做法** |

**關鍵洞見**：RWX 對「模型分發」其實**不是好選擇**。模型權重是唯讀、大檔、順序讀取 —— 每次 pod 啟動都從 NFS 讀 200GB，會把網路打爆，而且冷啟動時間變成好幾倍。RWX 適合的是「需要多寫者共享的小檔案」。

**能講出「我用了 RWX，但我知道生產上該用 local PV + 預先分發，因為模型是唯讀大檔、NFS 會變成頻寬瓶頸」，比只會說「我用 NFS」強得多。**

### 3.3 這組取捨的物理基礎：EBS vs instance store

上面那張表不是 k8s 發明的取捨，**它是底層儲存的物理特性往上長出來的**。用 AWS 的兩種磁碟來理解最快，因為你在 Phase 1 就會同時碰到這兩個東西。

```
┌─────────────────────────────────────────┐
│  資料中心裡的某一台實體主機               │
│   ┌──────────────┐                      │
│   │ 你的 VM       │                      │
│   └──────┬───────┘                      │
│   ┌──────▼───────────────────────────┐  │
│   │  Instance store（NVMe）           │  │ ← 就插在這台主機的主機板上
│   └──────────────────────────────────┘  │
└──────────┬──────────────────────────────┘
           │ 網路
┌──────────▼──────────────────────────────┐
│  EBS —— 另一套獨立的儲存系統              │
│  有自己的生命週期、自己的 volume ID       │
└─────────────────────────────────────────┘
```

**Instance store 是那台主機的內建硬碟；EBS 是掛載過來的網路磁碟。** 所有差異都從這句推導：

| | **Instance store** | **EBS** |
|---|---|---|
| 是不是獨立資源 | 不是，是機型規格的一部分 | **是**，有自己的 volume ID |
| `reboot` | ✅ 保留 | ✅ 保留 |
| **`stop` → `start`** | ❌ **全毀** | ✅ 保留 |
| 主機硬體故障 | ❌ 全毀 | ✅ 保留 |
| 拆下來接到別台機器 | ❌ | ✅ detach / attach |
| 快照備份 | ❌ | ✅ snapshot |
| 擴容 | ❌ 機型決定，固定 | ✅ 線上擴大 |
| 速度 | 更快（無網路跳轉） | gp3 基準 3,000 IOPS / 125 MB/s |
| 計費 | 內含在機器價格裡 | 另計 $/GB-月，**機器 stopped 時照收** |

**為什麼 `reboot` 沒事但 `stop` 會全毀**（理解這點就全懂了）：

- `reboot` = 作業系統重開，**你還在同一台實體主機上**，硬碟沒動過
- `stop` = AWS 釋放那台實體主機給別人用
- `start` = 排程器重新找一台有空位的主機，**很可能不是原來那台** → 你拿到的是另一塊全新的空 NVMe

EBS volume 全程待在儲存系統裡沒動，所以不管你被放到哪台主機，`start` 之後都能掛回來。
（這也解釋了「stopped 的機器為什麼還要付 EBS 錢」—— 那塊磁碟真的還佔著空間，你付的是保留資料的費用。）

### 3.4 對應到 k8s 的儲存抽象

**這不是巧合 —— k8s 的儲存型別就是在包裝同一組取捨：**

| AWS | k8s 的對應 | 共同的性質 |
|---|---|---|
| **EBS** | 網路型 PV（EBS CSI、Ceph RBD、iSCSI） | 可以跟著 pod 換節點；但**一次只能掛一個節點** → Part 2 在這種儲存上就是 `Multi-Attach error` |
| **Instance store** | **local PV / hostPath** | 快，但綁死在那個節點上。節點沒了資料就沒了 → kind 的 local-path 就是這種，所以 Part 2 在 kind 上是 `volume node affinity conflict` |
| — | `volumeBindingMode: WaitForFirstConsumer` | 專門為了解決「local PV 在 A 節點但 pod 被排到 B 節點」 |

所以 3.2 那張「模型 200GB × 5 個節點怎麼分發」的表，本質上就是這組取捨放大到叢集尺度：

- **每節點 local PV + 預先 rsync** = instance store 思路 —— 最快，但要自己管一致性與空間回收
- **RWX 網路共享（NFS / CephFS）** = EBS 思路 —— 省空間、可移動，但慢，而且會變成頻寬瓶頸與單點故障

**面試時可以這樣收束**：
> 「模型分發的取捨其實是儲存局部性（locality）與共享性之間的老問題。本地磁碟快但綁節點，網路儲存可共享但慢 —— LLM 的模型權重是唯讀大檔、順序讀取、而且只在 pod 啟動時讀一次，這個存取模式偏向『本地 + 預先分發』，所以我會用內部物件儲存加 initContainer 拉到 local PV，而不是把 200GB 掛在 NFS 上讓每個 pod 每次啟動都重讀一遍。」

### 3.5 `volumeBindingMode` 的坑

```yaml
volumeBindingMode: WaitForFirstConsumer   # vs Immediate
```

| 模式 | 行為 | 風險 |
|---|---|---|
| `Immediate` | PVC 一建立就配置 PV | **PV 可能被綁到沒有 GPU 的節點上**，然後 pod 永遠排不進去 |
| `WaitForFirstConsumer` | 等到有 pod 要用才配置，配置在那個 pod 被排到的節點 | 幾乎總是對的 |

**對 local PV 而言，`WaitForFirstConsumer` 是必須的**，否則儲存和運算會被綁在不同節點上。這在 Phase 3（GPU 節點）會變成真實的問題。

---

## Part 4：MetalLB —— on-prem 的 LoadBalancer

**目的**：OrbStack 直接給你 LoadBalancer，讓你學不到 on-prem 的真實狀況。

### 4.1 先看問題

```bash
kubectl -n pricer expose deploy/pricer-api --type=LoadBalancer --name=pricer-lb --port=8000
kubectl -n pricer get svc pricer-lb
```

`EXTERNAL-IP` 永遠是 `<pending>`。**因為 LoadBalancer 這個類型本身不做任何事** —— 它只是在請求一個「雲端供應商控制器」去配置外部負載平衡器。on-prem 沒有那個東西。

### 4.2 做法

```bash
# 查目前的 MetalLB 版本
curl -s https://api.github.com/repos/metallb/metallb/releases/latest \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['tag_name'])"

# 用上面查到的版本號取代 <VERSION>
kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/<VERSION>/config/manifests/metallb-native.yaml
kubectl -n metallb-system wait --for=condition=Available deploy/controller --timeout=180s
```

**關鍵步驟：IP 池必須落在 kind 的 Docker 網段裡**

```bash
docker network inspect kind -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}'
# 通常是 172.18.0.0/16
```

```bash
kubectl apply -f - <<'EOF'
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: kind-pool
  namespace: metallb-system
spec:
  addresses:
    # ⚠️ 必須在上面查到的網段內，而且要避開 kind 自己用的低位址
    - 172.18.255.200-172.18.255.250
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: kind-l2
  namespace: metallb-system
spec:
  ipAddressPools: [kind-pool]
EOF

kubectl -n pricer get svc pricer-lb    # EXTERNAL-IP 現在應該有值了
curl -s http://<那個IP>:8000/healthz
```

### 4.3 為什麼要懂這個

**on-prem 沒有雲端 LB，MetalLB 是事實標準。** 兩種模式：

| 模式 | 怎麼運作 | 限制 |
|---|---|---|
| **L2（ARP/NDP）** | 由一個節點回應該 IP 的 ARP 查詢 | **所有流量都經過那一個節點**，不是真正的負載平衡；故障轉移要幾秒 |
| **BGP** | 跟實體路由器對等，由路由器做 ECMP 分流 | 需要網路團隊配合設定 BGP |

**L2 模式最常被誤解的一點：它不是負載平衡，是故障轉移。** 頻寬上限就是那一個節點的網卡。需要真正的水平擴展就得用 BGP。這是很好的面試答案。

**`externalTrafficPolicy` 的取捨**：

| 值 | 來源 IP | 額外跳轉 | 適用 |
|---|---|---|---|
| `Cluster`（預設） | **會被 SNAT 蓋掉** | 可能多一跳 | 不在意來源 IP |
| `Local` | 保留真實來源 IP | 沒有額外跳轉 | 需要來源 IP 或在意延遲；但沒有該服務 pod 的節點會拒絕流量 |

---

## Part 5：排程 —— taint、toleration、affinity、spread

**目的**：單節點上「沒有選擇」，所以學不到排程。三節點才有得選。

### 5.1 nodeSelector 與 taint

```bash
# 把「GPU 節點」加上 taint，代表「非請勿入」
kubectl taint node pricer-lab-worker pricer.io/gpu=true:NoSchedule

# 現在 model-server 排不進去了
kubectl -n pricer rollout restart deploy/model-server
kubectl -n pricer get pods -o wide
kubectl -n pricer describe pod -l app=model-server | grep -A5 Events
# 0/3 nodes are available: 1 node(s) had untolerated taint ...
```

加上 toleration 與 nodeSelector：

```bash
kubectl -n pricer patch deploy/model-server --type=json -p='[
  {"op":"add","path":"/spec/template/spec/tolerations","value":[
    {"key":"pricer.io/gpu","operator":"Equal","value":"true","effect":"NoSchedule"}
  ]},
  {"op":"add","path":"/spec/template/spec/nodeSelector","value":{
    "node-role.pricer/gpu":"true"
  }}
]'
```

### 5.2 為什麼 taint 和 nodeSelector 要成對使用

**這是最常被搞混的一組概念，而且必須兩個都用：**

| 機制 | 方向 | 保證什麼 |
|---|---|---|
| **taint + toleration** | 節點 → pod | 「**沒有** toleration 的 pod 不准進來」 |
| **nodeSelector / affinity** | pod → 節點 | 「這個 pod **只想去** 有這個標籤的節點」 |

只有 toleration：GPU pod **可以**進 GPU 節點，但也可能被排到 CPU 節點上（然後找不到 GPU 而失敗）。
只有 nodeSelector：GPU pod 會去 GPU 節點，但一般 pod 也會被排進去，把昂貴的 GPU 節點填滿 CPU 工作。

**GPU 節點必須兩個都設。** 這在 Phase 3 是實際會用到的設定。

### 5.3 topologySpreadConstraints

```bash
kubectl -n pricer patch deploy/pricer-api --type=json -p='[
  {"op":"add","path":"/spec/template/spec/topologySpreadConstraints","value":[{
    "maxSkew":1,
    "topologyKey":"kubernetes.io/hostname",
    "whenUnsatisfiable":"ScheduleAnyway",
    "labelSelector":{"matchLabels":{"app":"pricer-api"}}
  }]}
]'
kubectl -n pricer get pods -l app=pricer-api -o wide
```

**`whenUnsatisfiable` 的取捨**：

| 值 | 排不下時 | 適用 |
|---|---|---|
| `DoNotSchedule` | pod 卡在 Pending | 分散是硬需求（例如跨可用區） |
| `ScheduleAnyway` | 盡力而為，排不下就擠一起 | **大部分情況**。可用性比完美分散重要 |

**跟 `podAntiAffinity` 的差別**：anti-affinity 是二元的（「不准在一起」），spread 是連續的（「盡量平均，容忍 maxSkew 的差距」）。節點數比 replica 數少的時候，`requiredDuringScheduling` 的 anti-affinity 會直接讓 pod 排不進去 —— 你在 Part 2 已經看過這個行為。

---

## Part 6：cordon / drain / PDB 與那個真實的兩難

**目的**：這是 on-prem 最日常的動作，而且對 LLM 服務特別尖銳。

### 6.1 做法

```bash
kubectl apply -f k8s/governance/pdb.yaml
kubectl -n pricer get pdb

# 標記節點不再接受新 pod（現有的不動）
kubectl cordon pricer-lab-worker2
kubectl get nodes

# 清空節點上的 pod
kubectl drain pricer-lab-worker2 \
  --ignore-daemonsets \
  --delete-emptydir-data
```

### 6.2 現在製造那個兩難

```bash
kubectl -n pricer scale deploy/model-server --replicas=1
kubectl -n pricer patch pdb model-server --type=json -p='[
  {"op":"remove","path":"/spec/maxUnavailable"},
  {"op":"add","path":"/spec/minAvailable","value":1}
]'

# 找出 model-server 在哪個節點，然後 drain 它
NODE=$(kubectl -n pricer get pod -l app=model-server -o jsonpath='{.items[0].spec.nodeName}')
kubectl drain "$NODE" --ignore-daemonsets --delete-emptydir-data --timeout=60s
```

**你會看到 drain 永遠卡住**：

```
evicting pod pricer/model-server-xxx
error when evicting pods ... Cannot evict pod as it would violate the pod's disruption budget.
```

### 6.3 為什麼這無解（而這正是重點）

```
只有 1 個 replica
  + PDB 要求 minAvailable: 1
  = 那個 pod 既不能被驅逐，也沒有別的地方可去
```

**這不是設定錯誤，是真實的資源限制。** 三個選項，每一個都有代價：

| 選項 | 代價 |
|---|---|
| **維護窗口接受停機**（`--disable-eviction` 或暫時移除 PDB） | 1–3 分鐘的服務中斷（模型冷啟動） |
| **多一張 GPU** 讓 replica 能到 2 | 硬體成本。on-prem 買不起就是買不起 |
| **GPU time-slicing** 讓一張卡假裝成多張 | 沒有記憶體與故障隔離（Phase 3 會實測） |

**能講清楚這個取捨，比會寫 PDB 的 YAML 重要十倍。** 因為它證明你理解「on-prem 的容量是固定的」這個根本約束 —— 雲端可以再開一台，地端不行。

### 6.4 還原

```bash
kubectl uncordon pricer-lab-worker2
NODE=$(kubectl get nodes -o jsonpath='{.items[1].metadata.name}')
kubectl uncordon "$NODE"
kubectl -n pricer patch pdb model-server --type=json -p='[
  {"op":"remove","path":"/spec/minAvailable"},
  {"op":"add","path":"/spec/maxUnavailable","value":1}
]'
```

### ⚠️ 陷阱

- **`cordon` 和 `drain` 不一樣。** `cordon` 只是「不再排新的進來」，現有 pod 不動。`drain` 才會驅逐。實務上一定是先 cordon，確認沒問題再 drain。
- **`--ignore-daemonsets` 幾乎一定要加**，因為 DaemonSet 的 pod（CNI、kube-proxy、node exporter、GPU device plugin）本來就該留在節點上，不加的話 drain 會直接拒絕執行。
- **`--force` 會刪掉沒有控制器管理的裸 pod，而且它們不會被重建。** 這是資料遺失的來源，用之前先確認那些 pod 是什麼。
- **PDB 只擋「自願性中斷」**（drain、eviction API）。節點直接斷電、kubelet 掛掉這類非自願中斷，PDB 一點忙都幫不上。

---

## Part 7：資源治理 —— ResourceQuota 與 LimitRange

**目的**：on-prem 的核心政治問題是「固定的資源怎麼分」。這是雲端職缺較少碰、地端職缺天天碰的東西。

### 7.1 做法

```bash
kubectl apply -f k8s/governance/quota.yaml
kubectl -n pricer describe resourcequota pricer-quota
```

試著超出配額：

```bash
# 先 cordon GPU 節點：新 pod 會被「建立」（佔配額）但排不上去，
# 這樣不會真的多跑好幾個 4.5 GiB 的 llama-server 把 VM 撐爆
kubectl cordon pricer-lab-worker

kubectl -n pricer scale deploy/model-server --replicas=5
kubectl -n pricer get pods -l app=model-server         # 1 Running + 幾個 Pending，湊不到 5 個
kubectl -n pricer get events --sort-by=.lastTimestamp | grep -i quota | tail -3
# ... exceeded quota: pricer-quota, requested: requests.memory=5Gi, used: requests.memory=..., limited: requests.memory=16Gi

# 做完縮回來 —— 後面的 Part 都假設只有 1 個 model-server
kubectl -n pricer scale deploy/model-server --replicas=1
kubectl uncordon pricer-lab-worker
```

注意兩件事：
- **Deployment 本身會建立成功，被擋下來的是 ReplicaSet 建 pod 的動作**。所以 `kubectl get deploy` 看起來正常，但 pod 數量就是上不去 —— 要看 events 才知道為什麼。
- **`Pending` 的 pod 也算配額。** 那幾個排不上去的 pod 沒在跑，但已經佔掉 `requests.memory` 了。配額算的是「宣告要多少」，不是「實際用多少」。

### 7.2 為什麼 ResourceQuota 一定要配 LimitRange

**這是一個會讓人困惑很久的互動**：

一旦 namespace 有了 `ResourceQuota` 且限制了 `requests.cpu` / `requests.memory`，**所有沒有寫 requests 的 pod 都會被直接拒絕**：

```
Error: pods "xxx" is forbidden: failed quota: must specify requests.cpu
```

`LimitRange` 的 `defaultRequest` 就是來補這個洞的 —— 讓沒寫的人自動拿到預設值，而不是每個人都要在每個 manifest 裡寫一遍。

**另一個重點：`LimitRange.max` 防止單一 pod 吃光整個配額。** 沒有它的話，一個手滑寫成 `memory: 100Gi` 的 pod 會讓整個 namespace 的其他人都排不進去。

### 7.3 GPU 配額

```yaml
requests.example.com/gpu: "2"
```

**Extended resource（GPU 就是一種）的特殊規則**：
- 只能用整數，不能是 `0.5`
- `requests` 必須**等於** `limits`（k8s 自動幫你補齊）
- 不能超賣 —— 這跟 CPU 完全不同，CPU 可以 request 少於 limit

Phase 0.5 用假的 `example.com/gpu` 練（見 Part 8），Phase 3 換成 `nvidia.com/gpu`，**其他設定一行都不用改**。

---

## Part 8：用假的 extended resource 零成本練 GPU 排程

**目的**：這是這個 Phase 投報率最高的技巧 —— **排程、配額、優先級、搶佔的全部語義，都能在沒有 GPU 的機器上練完。**

### 8.1 做法

```bash
kubectl proxy --port=8001 &
sleep 2

# 給「GPU 節點」加上 4 張假的卡
curl -s --header "Content-Type: application/json-patch+json" \
  --request PATCH \
  --data '[{"op":"add","path":"/status/capacity/example.com~1gpu","value":"4"}]' \
  http://localhost:8001/api/v1/nodes/pricer-lab-worker/status > /dev/null

kubectl describe node pricer-lab-worker | grep -A8 "Capacity:"
# 你會看到 example.com/gpu: 4
```

> `~1` 是 JSON Pointer 對 `/` 的跳脫寫法。`example.com/gpu` 要寫成 `example.com~1gpu`。

現在讓 pod 去要它：

```bash
kubectl -n pricer patch deploy/model-server --type=json -p='[
  {"op":"add","path":"/spec/template/spec/containers/0/resources/limits/example.com~1gpu","value":"1"}
]'
kubectl -n pricer get pods -o wide
```

試著要超過剩下的卡數。model-server 已經佔了 1 張，剩 3 張 —— 用一個輕量 pod 去要 4 張：

```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata: { name: gpu-hog, namespace: default }
spec:
  tolerations:
    - { key: pricer.io/gpu, operator: Equal, value: "true", effect: NoSchedule }
  containers:
    - name: sleep
      image: busybox:1.37
      command: ["sleep", "3600"]
      resources:
        limits: { example.com/gpu: "4" }
EOF
kubectl describe pod gpu-hog | grep -A3 "FailedScheduling"
# 0/3 nodes are available: 1 Insufficient example.com/gpu, ...
kubectl delete pod gpu-hog
```

**為什麼不用 `scale deploy/model-server --replicas=5`**：每多一個 replica 就多一個 4.5 GiB 的 llama-server，VM 會先被記憶體撐爆，你看到的會是記憶體問題而不是 GPU 排程。busybox 幾乎不吃記憶體，只考驗 GPU 這一個變數。

**為什麼放在 `default` namespace**：`pricer` 有 Part 7 的 ResourceQuota，只允許 2 張 GPU。把上面的 `namespace` 改成 `pricer` 再 apply 一次，你會看到**完全不同的失敗** —— pod 根本建不起來：

```
Error from server (Forbidden): ... exceeded quota: pricer-quota, requested: requests.example.com/gpu=4, ...
```

| | 擋在哪一關 | pod 物件存在嗎 | 在哪裡看 |
|---|---|---|---|
| `exceeded quota` | **admission**（API server 收件時） | ❌ 不存在 | `kubectl apply` 的錯誤、ReplicaSet 的 events |
| `Insufficient example.com/gpu` | **scheduling**（找節點時） | ✅ 存在，卡 `Pending` | `kubectl describe pod` |

這個區別在 Part 9 會直接決定搶佔能不能發生。

### 8.2 為什麼這值得做

**你可以練到的**（全部零成本）：
- extended resource 的整數限制、requests 必須等於 limits、不可超賣
- 排程器在資源不足時的行為與 `FailedScheduling` 訊息怎麼讀
- `ResourceQuota` 對 GPU 的限制
- `PriorityClass` 的搶佔在 GPU 稀缺時的行為（Part 9）
- PDB 與 drain 在「資源不可替代」時的互動

**你練不到的**：驅動、CUDA、記憶體隔離、真實的 GPU 效能。那些是 Phase 3 的事。

**而且切換成本是零** —— Phase 3 只要把 `example.com/gpu` 換成 `nvidia.com/gpu`，其他一行都不用改。

### ⚠️ 陷阱

- **這是假的。** 文件裡、面試時都要明講「我用假的 extended resource 練 scheduler 語義，真的 GPU 在 Phase 3」。假裝自己有 GPU 叢集是會被問穿的。
- **kubelet 重啟後這個容量可能會消失**，再 PATCH 一次就好。
- **記得收掉 `kubectl proxy`**：`kill %1`。

---

## Part 9：PriorityClass 與 preemption

**目的**：on-prem AI 平台最經典的面試題 —— 線上推論和離線 batch 搶同一張卡怎麼辦。

### 9.1 做法

**前提**：Part 8 的 4 張假 GPU 還在，model-server 佔著其中 1 張（`kubectl describe node pricer-lab-worker | grep example.com/gpu` 確認）。

```bash
# 先拿掉 Part 7 的 ResourceQuota（LimitRange 留著）—— 理由見下
kubectl -n pricer delete resourcequota pricer-quota

kubectl apply -f k8s/governance/priorityclass.yaml
kubectl get priorityclass
```

**為什麼要先拿掉配額**：配額擋在 admission，搶佔發生在 scheduling（Part 8 那張表）。`pricer` 的配額只允許 2 張 GPU —— batch 佔滿配額之後，model-server 的新 pod **連建立都建立不了**，scheduler 根本看不到它，也就不會為它搶佔。**搶佔救不了配額。** 這在真實平台上是很常見的誤會：「我設了高優先級，為什麼還是起不來？」—— 先看是不是卡在 quota。

先讓低優先的 batch job 把卡佔滿：

```bash
kubectl apply -f k8s/governance/batch-eval-job.yaml
kubectl -n pricer get pods -l job-name=batch-eval -o wide
# 3 個 batch pod Running（parallelism: 3，各佔 1 張）+ model-server 的 1 張 = 4 張全滿
kubectl describe node pricer-lab-worker | grep -A8 "Allocated resources" | grep example.com/gpu
```

再讓高優先的推論服務進來要卡：

```bash
# 只改優先級、不加 replica。改 pod template 會觸發 rollout（maxSurge: 1），
# 新 pod 要 1 張卡，但 4 張全滿 → scheduler 踢掉一個 batch pod
kubectl -n pricer patch deploy/model-server --type=json -p='[
  {"op":"add","path":"/spec/template/spec/priorityClassName","value":"inference-critical"}
]'

# 看搶佔發生
kubectl -n pricer get events --sort-by=.lastTimestamp | grep -i preempt
kubectl -n pricer get pods -l job-name=batch-eval -w
```

**為什麼不順便把 replicas 調到 3**：每個 replica 都是一個 4.5 GiB 的 llama-server，rollout 期間新舊並存已經是兩個，再加就超過 12 GiB 的 VM 了。一個新 pod 就足以觸發搶佔。

**接著觀察被踢掉之後發生什麼**：Job 會補一個新的 batch pod，但它卡在 `Pending` —— 優先級低、而且 `preemptionPolicy: Never`，不會反過來搶。等舊的 model-server pod 關掉、釋出那張卡，它才排進去。**這就是「有空位就做，沒空位就等」的實際樣子。**

你會看到類似：
```
Normal  Preempted  pod/batch-eval-xxx  Preempted by pod pricer/model-server-xxx on node ...
```

### 9.2 為什麼這樣設計

```yaml
# 高優先：可以搶別人
name: inference-critical
value: 1000000
preemptionPolicy: PreemptLowerPriority

# 低優先：自己不搶別人
name: batch-best-effort
value: 100
preemptionPolicy: Never          # ← 這行很重要
```

**`preemptionPolicy: Never` 為什麼關鍵**：不設的話，兩個 batch job 會**互相搶佔** —— A 踢掉 B，B 重新排程時踢掉 A，永遠沒有人做得完。這是典型的活鎖。

低優先的工作應該是「有空位就做，沒空位就等」，而不是加入搶位遊戲。

### 9.3 這題為什麼值得練

**單張 GPU 就能真的演。** 這是所有 GPU 相關的 k8s 主題裡，唯一不需要多張卡就能完整示範的（這裡用的 4 張假卡只是為了讓 batch 有地方跑）。Phase 3 只有一張 L4，但搶佔可以完整重現。

面試答案的形狀：
> 「我把線上推論設成高優先、離線評估設成低優先且不搶佔。卡不夠時 batch 會被踢掉，它是可重跑的所以沒關係。這樣同一張卡白天服務線上流量、晚上跑評估，利用率上得去，而線上的 SLA 不會被 batch 影響。」

### ⚠️ 陷阱

- **搶佔是「刪除」不是「暫停」。** 被踢掉的 pod 是被殺掉、從頭重來。所以低優先的工作必須是可重跑的，而且最好有 checkpoint。
- **`globalDefault: true` 只能有一個 PriorityClass。** 設了之後所有沒指定優先級的 pod 都會拿到它，影響範圍很大，這個 lab 兩個都設 `false`。
- **PDB 會擋搶佔嗎？** 排程器會**盡量**尊重 PDB，但在找不到其他方案時仍會違反它。PDB 對搶佔是「盡力而為」，不是硬保證。

---

## Part 10：Job / CronJob 做離線評估

**目的**：batch 推論是 LLM 平台的真實需求（離線評估、資料集標註、embedding 重算），而且它天然就是上一節那個被搶佔的角色。

### 10.1 做法

看 `k8s/governance/batch-eval-job.yaml` 的四個關鍵欄位：

```yaml
completions: 60        # 總共要完成 60 個分片
parallelism: 3         # 同時跑 3 個
backoffLimit: 4        # 最多容忍 4 次失敗
activeDeadlineSeconds: 1800   # 超過 30 分鐘直接砍
ttlSecondsAfterFinished: 600  # 完成 10 分鐘後自動清掉
```

```bash
kubectl -n pricer delete job batch-eval --ignore-not-found
kubectl apply -f k8s/governance/batch-eval-job.yaml
kubectl -n pricer get job batch-eval -w
```

> 這個 Job 的每個 pod 要 1 張假 GPU（為了 Part 9 的搶佔）。如果 pod 卡 `Pending` 且訊息是 `Insufficient example.com/gpu`，多半是 kubelet 重啟後假容量消失了 —— 照 Part 8 再 PATCH 一次。

### 10.2 為什麼用 Job 而不是自己寫迴圈

| 你自己寫迴圈 | k8s Job |
|---|---|
| 跑到一半機器掛了 → 全部重來 | 只有失敗的分片重跑 |
| 平行度要自己管 | `parallelism` 一行 |
| 失敗重試要自己寫 | `backoffLimit` |
| 卡住的話要自己發現 | `activeDeadlineSeconds` 自動砍 |
| 結果散落各處 | 統一在 Job 物件裡 |

**`ttlSecondsAfterFinished` 特別值得提**：沒有它的話，完成的 Job 物件會一直累積在 etcd 裡。一個每小時跑一次的 CronJob，一年就是 8760 個 Job 物件。這是真實會發生的 etcd 膨脹來源。

### 10.3 CronJob 的兩個陷阱

如果改成 CronJob（例如每晚跑評估）：

```yaml
concurrencyPolicy: Forbid    # 上一輪還沒跑完就不要開新的
startingDeadlineSeconds: 300 # 錯過排程時間超過 5 分鐘就放棄這一輪
```

- **`concurrencyPolicy` 預設是 `Allow`。** 評估跑 2 小時但排程是每小時一次的話，會越積越多直到把叢集壓垮。
- **`startingDeadlineSeconds` 不設的話**，叢集停機一天後恢復，會一口氣補跑所有錯過的排程。

---

## Part 11：NetworkPolicy（視 CNI 而定）

**先驗證你的 CNI 到底會不會執行它**（做法同 Phase 0 Part 2 的那段測試）。

kind 的預設 CNI（kindnet）**不強制執行 NetworkPolicy**。要練的話得換 Calico：

```bash
kind delete cluster --name pricer-lab
# 把 k8s/kind/cluster.yaml 裡 networking 那兩行取消註解
kind create cluster --config k8s/kind/cluster.yaml
kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.29.1/manifests/calico.yaml
```

### LLM 服務的 NetworkPolicy 該長什麼樣

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: model-server-lockdown
  namespace: pricer
spec:
  podSelector:
    matchLabels: { app: model-server }
  policyTypes: ["Ingress", "Egress"]
  ingress:
    - from:
        - podSelector:
            matchLabels: { app: pricer-api }    # 只有 pricer-api 能打進來
      ports:
        - port: 8080
  egress:
    - to:                                        # DNS 一定要放行
        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: kube-system }
      ports:
        - { protocol: UDP, port: 53 }
        - { protocol: TCP, port: 53 }
```

**為什麼 model-server 適合幾乎完全封鎖**：權重下載是**啟動時一次性**的（而且我們是在 initContainer 裡做）。服務跑起來之後，它不該有任何對外連線。這對 on-prem 的合規與 air-gap 情境非常對味，也是很好的面試答案。

### ⚠️ 最危險的陷阱

**CNI 不執行 NetworkPolicy 時，`kubectl apply` 會成功、不會有任何錯誤、什麼事也不會發生。** 你以為自己學會了，其實只是寫了一份被忽略的 YAML。**測不出效果就不要宣稱會這個。**

---

## Phase 0.5 驗收清單

- [ ] 三節點叢集跑起來，`kubectl get nodes` 看到 3 個 Ready
- [ ] **親眼看到 RWO 讓另一個節點的 pod 起不來**，並記下實際的錯誤訊息（kind 上預期是 `volume node affinity conflict`；這是本 Phase 最重要的一項）
- [ ] RWX 的 PVC 讓兩個 replica 分散在不同節點上都掛載成功
- [ ] MetalLB 給 LoadBalancer 發出真實 IP，且 `curl` 得通
- [ ] taint 造成 `FailedScheduling`，加上 toleration + nodeSelector 後恢復
- [ ] `drain` 被 PDB 擋住（單 replica + minAvailable: 1）
- [ ] 超出 ResourceQuota 時 pod 被拒絕，events 裡看得到 `exceeded quota`
- [ ] 假的 `example.com/gpu` 讓 pod 在超額時 `FailedScheduling`，並對照過 `exceeded quota` 的差別
- [ ] **`kubectl get events` 看到 `Preempted`**
- [ ] batch Job 的 `completions` / `parallelism` 正常分片完成
- [ ] 已確認並記錄 kind 的 CNI 是否執行 NetworkPolicy

---

## 收工

```bash
kind delete cluster --name pricer-lab
orb config set memory_mib 8192            # 調回原本的值（Part 1 記下的那個）
orb stop && orb start
```

**AWS 的 GPU 配額這時候應該已經下來了。** 查一下：

```bash
aws service-quotas get-service-quota \
  --service-code ec2 --quota-code L-DB2E81BA \
  --region us-east-1 --query 'Quota.Value'
```

---

## 下一站

→ [`04-phase1-vllm-gpu.md`](04-phase1-vllm-gpu.md)：第一次花錢。租一台 GPU EC2，用**純 Docker**（刻意先不上 k8s）把 vLLM 跑起來，並驗證 MAE 對得上 $39.85。
