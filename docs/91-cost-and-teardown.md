# LLM Inference on Kubernetes 實驗室 — 91：成本控管與收工檢查

> 這一份是**速查卡**，不是教學。開機前掃一眼，離桌前照著做。
> 整個 lab 的目標成本是 **$60 以內**。失控的唯一原因是「忘記關」。

---

## 離桌檢查清單（每一次，沒有例外）

```bash
# 1. 停掉 GPU 機器
aws ec2 stop-instances --region us-east-1 --instance-ids <ID>

# 2. 確認真的沒有東西在跑
aws ec2 describe-instances --region us-east-1 \
  --filters "Name=instance-state-name,Values=running,pending" \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,State.Name,LaunchTime]' \
  --output table
```

**空的表格 = 安全。** 有東西 = 立刻停掉。

把這個存成一個 alias，讓它變成肌肉記憶：

```bash
echo "alias labdown='aws ec2 stop-instances --region us-east-1 --instance-ids <ID> && aws ec2 describe-instances --region us-east-1 --filters \"Name=instance-state-name,Values=running,pending\" --query \"Reservations[].Instances[].[InstanceId,State.Name]\" --output table'" >> ~/.zshrc
```

---

## 每日對帳

```bash
aws ce get-cost-and-usage \
  --time-period Start=$(date -v-7d +%Y-%m-%d),End=$(date +%Y-%m-%d) \
  --granularity DAILY --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=SERVICE \
  --query 'ResultsByTime[].{Date:TimePeriod.Start,Items:Groups[?Metrics.UnblendedCost.Amount!=`0`].[Keys[0],Metrics.UnblendedCost.Amount]}' \
  --output json
```

> macOS 的 `date -v-7d`；Linux 用 `date -d '7 days ago' +%Y-%m-%d`。

**正常的一天應該只有 EC2 和 EBS 兩項。** 出現你不認識的服務就去查。

---

## 各階段的預期成本

| 階段 | 資源 | 單價 | 抓多久 | 小計 |
|---|---|---|---|---|
| Phase 0 | 本機 | $0 | — | **$0** |
| Phase 0.5 | 本機 | $0 | — | **$0** |
| Phase 1 | g6.xlarge | $0.8048/hr | 20 hr | ~$16 |
| Phase 2 | 同上 | $0.8048/hr | 15 hr | ~$12 |
| Phase 3 | 同上 | $0.8048/hr | 15 hr | ~$12 |
| 全程 | EBS 150GB gp3 | ~$12/月 | 1 個月 | ~$12 |
| Phase 4 | EKS（選作） | 見該文件 | — | ~$20 |
| | | | **主線合計** | **~$52** |

**「抓多久」是累積開機時數，不是連續開著。** 分很多次做，每次收工 `stop`，帳單只算 `running` 的時數。

時數的大宗不是 GPU 在算，是**載入模型的固定成本 × 試錯次數** —— 每改一次 vLLM 旗標就要重啟、重載權重、重跑一輪驗證，Phase 1 光是對照表就有四組配置。所以這些數字是把試錯估寬鬆的上限，不是目標。

**能壓的部分**：這裡面有一大塊是你在讀 log、想、打字，GPU 全程閒置但照樣 $0.80/hr。能在本機做的（寫 YAML、讀文件、規劃下一組參數）就別開著機器做 —— 這正是 Phase 0 / 0.5 兩個零成本階段存在的理由。

**Spot 可以再砍 60–70%**，代價是隨時可能被中斷。學習階段可以接受（模型快取在 EBS 上，損失有限）。

---

## 三種「沉默的漏財」

這三個不會出現在「有機器在跑嗎」的檢查裡，但會一直收費：

### 1. 閒置的 EBS volume

```bash
aws ec2 describe-volumes --region us-east-1 \
  --filters "Name=status,Values=available" \
  --query 'Volumes[].[VolumeId,Size,CreateTime]' --output table
```

`available` 狀態 = 沒有掛在任何機器上 = 純浪費。terminate instance 時如果 `DeleteOnTermination` 是 false，volume 就會留下來。

### 2. 未綁定的 Elastic IP

```bash
aws ec2 describe-addresses --region us-east-1 \
  --query 'Addresses[?AssociationId==null].[PublicIp,AllocationId]' --output table
```

**2024/2/1 起 AWS 對所有 public IPv4 收 $0.005/hr**（約 $3.6/月），綁著也要錢 —— 舊文章常寫的「綁著免費、閒置才收費」已經不適用了。

金額不大，但閒置的 EIP 是純浪費：沒有任何機器在用，你還在付。terminate 之後記得一起 release。

### 3. NAT Gateway 與 Load Balancer（只有做 Phase 4 才會有）

```bash
aws ec2 describe-nat-gateways --region us-east-1 \
  --filter "Name=state,Values=available" --query 'NatGateways[].NatGatewayId'
aws elbv2 describe-load-balancers --region us-east-1 --query 'LoadBalancers[].LoadBalancerName'
```

NAT Gateway 是 $0.045/hr + 流量費，**忘記刪就是每月約 $32**。

---

## 一鍵健檢

把這個存成 `scripts/aws-check.sh`：

```bash
#!/usr/bin/env bash
R=${1:-us-east-1}
echo "=== 執行中的機器 ==="
aws ec2 describe-instances --region "$R" \
  --filters "Name=instance-state-name,Values=running,pending" \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,State.Name]' --output table
echo "=== 閒置的 EBS ==="
aws ec2 describe-volumes --region "$R" --filters "Name=status,Values=available" \
  --query 'Volumes[].[VolumeId,Size]' --output table
echo "=== 未綁定的 EIP ==="
aws ec2 describe-addresses --region "$R" \
  --query 'Addresses[?AssociationId==null].[PublicIp]' --output table
echo "=== NAT Gateway ==="
aws ec2 describe-nat-gateways --region "$R" --filter "Name=state,Values=available" \
  --query 'NatGateways[].[NatGatewayId]' --output table
echo "=== Load Balancer ==="
aws elbv2 describe-load-balancers --region "$R" \
  --query 'LoadBalancers[].[LoadBalancerName,Type]' --output table
```

**五個區塊全空 = 你今天不會被收錢。**

---

## stop / terminate / delete 的差別

| 動作 | EC2 實例 | EBS root volume | **Instance store**（機型自帶的 NVMe） | 何時用 |
|---|---|---|---|---|
| **stop** | 停止計費 | **繼續計費**（約 $12/月），資料保留 | ⚠️ **資料全部消失** | 每天收工 |
| **terminate** | 消失 | 隨之刪除（若 `DeleteOnTermination=true`） | 消失 | 整個階段做完、數據已抓回本機 |

⚠️ **Instance store 連 `stop` 都保不住。** 它綁在實體主機上，`start` 之後你會被放到另一台機器。所以 HF cache、benchmark 結果這些都要放 EBS（`$HOME` 底下），instance store 只放丟了也無所謂的暫存。

**stop 是日常，terminate 是收工。** 在 terminate 之前一定要先把 benchmark 數據 `scp` 回本機 —— 機器刪了就沒了。

---

## 預算警示（Phase 0 就該設好）

| 名稱 | 類型 | 金額 | 用途 |
|---|---|---|---|
| `lab-monthly-hard` | 月 | $80 | 總量控制 |
| `lab-daily-canary` | 日 | $5 | **忘記關機的煙霧偵測器** |

**日預算才是真正有用的那個。** 正常使用一天不會超過 $5，一收到日警示就代表有東西忘了關。

**兩個都不要加 filter**，範圍保持「全部服務、全部 region」。限縮成 EC2 就會漏掉 EKS 控制平面、S3、CloudWatch 這些你沒預期到的項目 —— 而會咬人的永遠是沒預期到的那一項。理由詳見 [`01-setup.md`](01-setup.md) Part 2。

⚠️ AWS 的帳務資料有數小時延遲，**預算警示不是即時的**。它是安全網，不是關機開關。真正的防護是上面那份離桌檢查清單。
