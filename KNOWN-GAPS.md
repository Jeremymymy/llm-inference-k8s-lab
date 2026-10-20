# 已知的邊界

> 這份文件本身就是交付項目之一。
> **知道自己不會什麼，比假裝什麼都會有價值。** 面試時被問到缺口，這裡就是答案。

格式：每一項三行 —— **它解決什麼問題 / 我會怎麼做 / 為什麼這次沒做**。

---

## 一、三個最大的結構性缺口

### 1. 沒有真正的多節點推論

**它解決什麼問題**：單張 GPU 放不下的模型（70B 以上），需要跨卡甚至跨機的 tensor / pipeline parallel。

**我會怎麼做**：用 **LeaderWorkerSet**（不是 StatefulSet —— LWS 才是 k8s 上多節點推論的實際答案）搭配 Ray 做編排。但第一步不是寫 YAML，是**量 NCCL all-reduce 的頻寬** —— 節點間頻寬決定了 TP 和 PP 該怎麼切。NVLink 內部可以放心 TP，跨機走乙太網路就要考慮 PP。還要處理 NUMA 與 PCIe 拓撲的 GPU affinity，以及 RDMA / InfiniBand 的設定。

**為什麼這次沒做**：只有一張 L4。租多卡機器（p4d / p5）的成本遠超這個 lab 的預算，而且一小時的價格比整個 lab 還貴。

---

### 2. 沒有 day-2 operations 的時間深度

**它解決什麼問題**：真實的平台會活好幾年，要處理版本升級、驅動升級、備份還原、真實事故。

**我會怎麼做**：
- **k8s minor 升級**：control plane 先升，再逐節點 cordon → drain → 升 kubelet → uncordon。GPU 節點特別麻煩，因為 drain 會造成服務中斷（見 Phase 3 的兩難）
- **GPU driver 升級**：要 drain 整個節點，且 GPU Operator 的驅動容器與主機驅動不能並存
- **etcd 備份還原**：`etcdctl snapshot save` 排程 + 定期演練還原（**沒演練過的備份等於沒有備份**）
- **容量規劃**：用 DCGM 的歷史利用率推估

**為什麼這次沒做**：一個月的 lab 產不出「跑了三個月」的經驗。**但我做了四份事故演練紀錄**（見 detail 分支的 [`POSTMORTEMS.md`](https://github.com/Jeremymymy/llm-inference-k8s-lab/blob/detail/POSTMORTEMS.md)；終端輸出為事後重建）來補這一塊 —— 那是我能在短時間內產出的最接近 day-2 的東西。

---

### 3. 規模與多租戶是玩具等級

**它解決什麼問題**：真實平台要同時服務多個團隊、多個模型、多個版本，並且要能歸屬成本。

**我會怎麼做**：
- 隔離：namespace + RBAC + ResourceQuota + NetworkPolicy（這些我做了）**再加上** 完整的身分串接與稽核（沒做）
- 成本歸屬：DCGM 的 per-pod `SM_ACTIVE` 累計（有雛形，沒做成計費）
- 模型版本管理：canary / A/B 流量切分（完全沒做）

**為什麼這次沒做**：單一模型、單一使用者的 lab 環境造不出真實的資源競爭。**我做到的最接近的是多 LoRA 熱插拔**（一個 base 服務多個 adapter），那是 on-prem 最常見的省 GPU 多租戶架構的技術基礎。

---

## 二、GPU 相關

| 項目 | 解決什麼 | 我會怎麼做 | 為什麼沒做 |
|---|---|---|---|
| **MIG** | 真正的硬體隔離，SLA 敏感的多租戶 | `nvidia-smi mig` 切分 + GPU Operator 的 MIG 策略 | **L4 是 Ada 架構，不支援 MIG**（只有 A100/H100/H200 那條線有；Phase 3 節點標籤 `nvidia.com/mig.capable=false`、`mps.capable=false`）。我只做過 time-slicing，能講差異但沒跑過 |
| **MPS** | 記憶體軟上限，比 time-slicing 好一點的隔離 | CUDA MPS daemon + device plugin 設定 | 同上，且效益介於兩者之間、複雜度不低 |
| **多 GPU 節點的排程** | 真實的排程決策（有得選才叫排程） | node affinity + GPU 型號標籤 | 只有一台機器一張卡 |
| **NVLink / GPUDirect** | 跨卡通訊頻寬 | 拓撲感知排程 | 需要多卡 |
| **HPA 接 vLLM 指標實跑** | 依排隊長度自動擴容 GPU pod | prometheus-adapter 把 `vllm:num_requests_waiting` 映射成 `pricer_requests_waiting`（設計已完成，HPA YAML 不用改）；`maxReplicas` 壓到 2，門檻用 30 req/s 轉折點時的穩態排隊長度校正 | **HPA 接 vLLM 指標只做到設計，未在 GPU 上實跑**：Phase 3 精簡路線跳過 Part 9（同一套 stack 在 Phase 0 已實跑），`hpa.yaml` 的 `averageValue: "2"` 也沒有實測依據 |

---

## 三、儲存與分發

| 項目 | 解決什麼 | 我會怎麼做 | 為什麼沒做 |
|---|---|---|---|
| **大模型的多節點分發** | 200GB 權重怎麼到 5 個節點 | 內部 MinIO/Ceph RGW + initContainer 拉到 local PV；用 `WaitForFirstConsumer` 綁定 | 我的模型只有 6.4GB，撞不到真正的痛點。**四種做法的取捨我寫在 Phase 0 Part 3**，但沒有實測過效能差異 |
| **CSI snapshot / 備份** | 資料保護 | VolumeSnapshot CRD | lab 的資料都可重建 |
| **儲存效能比較** | NFS vs local NVMe 的冷啟動差距 | 同一個模型三種掛法各量一次 | 需要多節點與真實儲存設備 |

---

## 四、網路與安全

| 項目 | 解決什麼 | 我會怎麼做 | 為什麼沒做 |
|---|---|---|---|
| **NetworkPolicy 實測** | 網路層隔離 | kind + Calico | ⚠️ **OrbStack 和 kindnet 都不強制執行 NetworkPolicy**（2026-09-26 實測 OrbStack 回傳 200）。我寫了 policy 也驗證了「CNI 到底執不執行」，但**沒有在會執行的 CNI 上實測過效果**。測不出效果就不宣稱會 |
| **mTLS / 服務網格** | 服務間加密與細粒度授權 | Linkerd（比 Istio 輕）| 對單一服務鏈是過度設計 |
| **air-gap 部署** | 無外網環境 | `skopeo copy` 搬 image + Harbor 當內部 registry + 模型走內部物件儲存 | 需要模擬隔離環境 |
| **Image 簽章與掃描** | 供應鏈安全 | cosign + Trivy in CI | 沒有 CI |
| **HA control plane** | 控制平面不是單點 | 3 台 master + etcd quorum | 單機 k3s |

---

## 五、推論進階題（未來展望）

> 這些是「還有時間就做」的清單，不是缺陷。

### prefix caching

**解決什麼**：我的 prompt 有固定前綴（`What does this cost to the nearest dollar?\n\n`）。理論上這段的 prefill 可以只算一次，後續請求共用 KV cache 的 block。

**我會怎麼做**：vLLM 開 `--enable-prefix-caching`，量前後的 TTFT 與吞吐。因為這是 **prefill 主導**的 workload，效益可能比一般 chat 場景還明顯 —— 這是個值得做的實驗。

**為什麼沒做**：固定前綴只有約 10 個 token，佔 150 token 的比例不高，預期效益有限。但值得量一次來確認這個假設。

### speculative decoding

**解決什麼**：用小模型先猜、大模型驗證，降低 decode 階段的延遲。

**我會怎麼做**：vLLM 的 `--speculative-model` 配一個 1B 的 draft model，量 TPOT 的改善。

**為什麼沒做**：**我的輸出只有 5 個 token**，decode 根本不是瓶頸。對這個 workload 來說 speculative decoding 幾乎沒有意義 —— 這本身就是個結論：**技術要看 workload 形狀，不是越新越好。**

### 多模型 routing

**解決什麼**：多個模型 / adapter 共存時，請求該送到哪個後端。

**我會怎麼做**：LiteLLM proxy 或 Envoy AI Gateway。關鍵是**路由依據不該是連線數**（LLM 的連線可能持續數分鐘，連線數最少的 pod 可能正在處理最重的請求），而該用 KV cache 使用率或佇列長度。

**為什麼沒做**：多 LoRA 熱插拔已經涵蓋了「一個服務多個模型」的核心；跨服務的 routing 需要多個後端才有意義。

### 完整的可觀測性

**已經做了**：Prometheus + Grafana + ServiceMonitor + prometheus-adapter custom metrics HPA + DCGM。

**還缺**：
- **分散式追蹤**（OpenTelemetry）—— 能看到單一請求在 pricer-api 與 model-server 之間的時間分布
- **結構化日誌聚合**（Loki）—— 目前只有 `kubectl logs`
- **告警規則**（Alertmanager）—— 有 dashboard 但沒有「出事會叫我」的機制
- **SLO 與錯誤預算** —— 有 p99 數字但沒有正式的 SLO 定義

**為什麼沒做**：這四項在單人的 lab 環境裡沒有接收端（沒有 on-call）。但**告警規則其實值得補**，它是從「我看得到」到「它會告訴我」的分界。

### EKS 實跑

**解決什麼**：雲端託管 k8s 的實際操作經驗。

**我會怎麼做**：見 detail 分支的 [`docs/07-phase4-eks.md`](https://github.com/Jeremymymy/llm-inference-k8s-lab/blob/detail/docs/07-phase4-eks.md) 的附錄。

**為什麼沒做（或延後）**：目標是 on-prem 職缺，EKS 獨有的東西（IRSA、Karpenter、ALB controller）在地端都不存在。**我做了雲端↔地端的對照表**，那對這個目標來說價值更高，而且省掉每月 $73 的控制平面租金。

---

## 六、這個 lab 沒有涵蓋的 k8s 主題

誠實列出來，被問到就直接承認：

- **GitOps（ArgoCD / Flux）** —— 我用 `kubectl apply -k`，沒有做宣告式的持續同步與 drift 自癒
- **Admission control** —— 知道 GPU Operator 靠 mutating webhook 注入環境變數（比對提交的 YAML 與實際跑的就看得到），但沒有寫過自己的 webhook 或 policy（Kyverno / OPA Gatekeeper）
- **Gateway API** —— 能講它解決 Ingress 的 annotation 地獄（timeout 是 HTTPRoute 的正式欄位，不是廠商字串；retry 截至 v1.6 仍在推進），也知道 ingress-nginx 已於 2026-03 退役、官方建議遷移到它，但沒有實作。lab 仍用 ingress-nginx 只為了簡便
- **Gateway API Inference Extension / llm-d** —— 知道它用 InferencePool + Endpoint Picker 依佇列長度、KV cache、LoRA 做 LLM-aware 路由（跟我 HPA 用的是同一類指標），但沒有實作；我的路由是 Service 的一般負載平衡
- **KEDA** —— 知道 on-prem 常用它做 queue-based scaling 和 scale-to-zero，比 prometheus-adapter 更常見，但我用的是後者
- **KServe / Ray Serve / KAITO** —— LLM 專用的 serving 框架，我是手刻 Deployment
- **CNI 選型與 MTU 調優** —— 用預設的
- **Cluster autoscaler** —— on-prem 沒有對應物（容量固定），雲端的我沒做
