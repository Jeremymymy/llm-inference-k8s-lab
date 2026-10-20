# LLM Inference on Kubernetes 實驗室 — 07：Phase 4，EKS（選作）

> 成本：**$0 如果只讀對照表**；約 $20 如果真的跑一輪。
> 定位：**對 on-prem 職缺而言，講得出這張對照表比真的開一次 EKS 更有價值。**

---

## 為什麼這一階段是選作

你要應徵的是 **on-prem AI** 職缺。EKS 教你的東西裡，真正獨特的部分（IRSA、Karpenter、ALB controller）**在地端都不存在**。

而且 EKS 的 control plane 要 $0.10/hr（約 $73/月），在學習階段是純租金。

**但「我知道雲端的對應物是什麼」仍然有價值** —— 面試官可能來自雲端背景，或公司在做混合雲。所以這一份的主體是對照表。

---

## Part 1：雲端 ↔ 地端對照表

**這張表是這一份文件的核心產出。把它背熟，比跑一次 EKS 有用。**

### 身分與權限

| EKS / 雲端 | on-prem 的對應 | 差異在哪 |
|---|---|---|
| **IRSA / Pod Identity**（pod 拿 IAM 角色） | ServiceAccount + RBAC + 外部 secret 管理（Vault / External Secrets Operator） | 雲端有一個天然的身分提供者；地端要自己架，或退回到「把憑證做成 Secret」 |
| IAM Role for node | 節點上的靜態憑證或 mTLS | 地端沒有 metadata endpoint 這種東西 |
| KMS 加密 etcd | 自己管金鑰（HSM / Vault） | 地端要自己處理金鑰輪替 |

**面試答案**：「地端沒有 IRSA 這種雲端身分機制，所以 ServiceAccount + RBAC 的權重比雲端更高，而且 `automountServiceAccountToken: false` 這種預設關閉的習慣更重要 —— 因為沒有第二層 IAM 幫你兜底。」

### 網路

| EKS / 雲端 | on-prem 的對應 | 差異在哪 |
|---|---|---|
| **AWS Load Balancer Controller**（Service type=LoadBalancer 自動開 ALB/NLB） | **MetalLB** + Gateway API 實作（例如 Envoy Gateway） | MetalLB 的 L2 模式**不是負載平衡，是故障轉移** —— 所有流量經過一個節點。要真正水平擴展得用 BGP 模式，而那需要網路團隊配合。L7 那層別再選 ingress-nginx：它已於 2026-03 退役（本 lab Phase 0 用它只為了簡便） |
| VPC CNI（pod 拿 VPC IP） | Calico / Cilium（overlay 或 BGP） | 地端要自己決定 overlay vs underlay，以及 MTU |
| Security Group for pods | NetworkPolicy | 地端只有 NetworkPolicy，而且**要確認 CNI 真的會執行它** |
| Route 53 | CoreDNS + 內部 DNS | |

### 儲存

| EKS / 雲端 | on-prem 的對應 | 差異在哪 |
|---|---|---|
| **EBS CSI**（動態配置區塊儲存） | local PV / Ceph RBD / 儲存陣列的 CSI driver | local PV 沒有動態配置，要自己準備；`volumeBindingMode: WaitForFirstConsumer` 是必須的 |
| **EFS CSI**（RWX） | NFS / CephFS | 地端的 NFS 常常是單點故障與頻寬瓶頸 |
| S3 + Mountpoint（模型權重） | MinIO / Ceph RGW | 這是模型分發最常見的地端解法 |
| EBS snapshot | 儲存設備自己的快照，或什麼都沒有 | |

### 容量

| EKS / 雲端 | on-prem 的對應 | 差異在哪 |
|---|---|---|
| **Karpenter / Cluster Autoscaler** | **不存在** | **這是雲端與地端最根本的差異**：地端的容量是固定的。你不能「不夠就再開一台」 |
| Spot instance | 不存在 | |
| 多可用區 | 機櫃 / 電源 / 交換器層級的故障域 | topologySpreadConstraints 的 topologyKey 要對應實體拓撲 |

**這一格是整張表最重要的。** 因為「容量固定」這個約束，直接導出地端平台的所有特色：

```
容量固定
  ├─► ResourceQuota 是硬需求（要分配有限的 GPU）
  ├─► PriorityClass + preemption 是硬需求（誰該被踢掉）
  ├─► GPU 共享（time-slicing / MIG）的價值遠高於雲端
  ├─► drain 與 PDB 的兩難無法用「多開一台」解決
  └─► 利用率監控（DCGM SM_ACTIVE）是真金白銀的問題
```

**面試答案**：「雲端的容量問題可以用 Karpenter 解決，地端不行 —— 所以地端平台工程師的重心在資源治理（配額、優先級、搶佔）和利用率，而不是彈性擴縮。我這個 lab 的 Phase 0.5 和 Phase 3 就是在練這些。」

### 監控與其他

| EKS / 雲端 | on-prem 的對應 |
|---|---|
| CloudWatch Container Insights | kube-prometheus-stack |
| ECR | Harbor（含 image 簽章與弱點掃描） |
| EKS 託管控制平面 | 自己管 3 台 master + etcd 備份還原 |
| EKS 版本升級 | 自己排 drain 順序，逐節點升 |

---

## Part 2：附錄 —— 如果你真的想跑一輪

**只在你已經做完 Phase 0 到 3 之後才做。** 一個週末的量。

### 2.1 最小成本的做法

```bash
brew install eksctl

eksctl create cluster \
  --name pricer-eks \
  --region us-east-1 \
  --version 1.31 \
  --nodegroup-name gpu-ng \
  --node-type g6.xlarge \
  --nodes 1 \
  --node-volume-size 150 \
  --managed
```

### 2.2 ⚠️ 三個會讓帳單失控的東西

| 項目 | 費用 | 怎麼避免 |
|---|---|---|
| **NAT Gateway** | $0.045/hr + $0.045/GB | eksctl **預設會開**。學習用叢集把節點放 public subnet 並關掉 NAT（查你這版 eksctl 的 `--vpc-nat-mode` 選項）。忘記刪就是每月約 $32 |
| **ALB** | $0.0225/hr + LCU | 用完就刪。ALB controller 建的 ALB **不會**隨 Ingress 刪除而自動消失（如果 finalizer 出問題） |
| **EKS control plane** | $0.10/hr | 這個沒得避，但用完立刻 `eksctl delete cluster` |

### 2.3 EKS 特有的東西（跑一輪才學得到的）

```bash
# GPU 節點需要 accelerated AMI，eksctl 通常會自動選，但要確認
kubectl describe node | grep -i "nvidia\|ami"

# NVIDIA device plugin（EKS 的 GPU AMI 已含驅動）
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/master/deployments/static/nvidia-device-plugin.yml

# 之後就跟 Phase 3 一模一樣 —— 同一個 gpu overlay 直接用
kubectl apply -k k8s/overlays/gpu
```

**注意 nodeSelector**：Phase 3 用的是 GPU Operator 打的 `nvidia.com/gpu.present=true`。EKS 上如果只裝 device plugin 沒裝 NFD，這個標籤不存在，要改成 `eks.amazonaws.com/nodegroup: gpu-ng` 或自己打標籤。**這種「同一份 manifest 在不同叢集要微調」正是 overlay 存在的理由。**

### 2.4 Pod Identity（IRSA 的後繼者）

```bash
eksctl create podidentityassociation \
  --cluster pricer-eks \
  --namespace pricer \
  --service-account-name pricer-api \
  --permission-policy-arns arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess
```

**值得做一次是為了體會差異**：雲端上，pod 透過 ServiceAccount 就自動拿到了雲端資源的權限，中間沒有任何憑證檔案。地端沒有這個 —— 你得把憑證做成 Secret（像我們對 HF token 做的那樣），然後自己處理輪替。

### 2.5 完整 teardown

```bash
# 1. 先刪 Ingress / Service type=LoadBalancer（不然 ALB/NLB 會變孤兒）
kubectl delete ingress --all -A
kubectl delete svc --all-namespaces --field-selector spec.type=LoadBalancer

# 2. 刪叢集
eksctl delete cluster --name pricer-eks --region us-east-1

# 3. ⚠️ 逐項確認沒有殘留
aws ec2 describe-nat-gateways --region us-east-1 \
  --filter "Name=state,Values=available" --query 'NatGateways[].NatGatewayId'
aws elbv2 describe-load-balancers --region us-east-1 --query 'LoadBalancers[].LoadBalancerArn'
aws ec2 describe-volumes --region us-east-1 \
  --filters "Name=status,Values=available" --query 'Volumes[].VolumeId'
aws ec2 describe-addresses --region us-east-1 \
  --query 'Addresses[?AssociationId==null].PublicIp'
```

**四個都要是空的。** 任何一個有東西就是在漏錢。

---

## 這一階段該產出的一句話

在 `92-project-narrative.md` 補上：

> 「我刻意沒有把 EKS 當主線 —— 我要做的是地端平台，而 GPU + k8s 的核心知識在單機 k3s 上完全學得到，還省掉每月 $73 的控制平面租金。我整理了一份雲端↔地端的對照表：IRSA 對應到 ServiceAccount 加外部 secret 管理、ALB controller 對應到 MetalLB 加 ingress、EBS CSI 對應到 local PV。最關鍵的一格是 Karpenter —— 地端沒有對應物，因為容量是固定的，這也是為什麼地端平台的重心在資源治理和利用率，而不是彈性擴縮。」
