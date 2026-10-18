# LLM Inference on Kubernetes 實驗室 — 04：Phase 1，雲端 GPU 上的 vLLM

> 成本：**約 $16**（g6.xlarge 累積開機 20 小時 —— 分多次做，每次收工就 `stop`）。這是第一次花錢的階段。
> **精簡路線**：跟 Phase 2 合併成一輪，粗估約 5–6 小時、約 $5（見下方「精簡路線」）。
> 產出：vLLM 服務化、正確性對齊 MAE $39.85、以及兩個原創實驗的數據。
> **刻意不上 k8s。** 理由見下。

---

## 這一階段的目的

**先單獨搞懂 inference 這一層。**

GPU 問題的標準排查順序是由下往上：

```
nvidia-smi 看得到卡嗎
   └─► docker run --gpus all 容器裡看得到嗎
          └─► device plugin 讓 k8s 看得到嗎
                 └─► pod 真的拿到卡了嗎
```

一開始就疊上 k8s，出問題時你會分不清是哪一層。**Phase 1 不是「退回」，是先確認 runtime 正確再往上包一層** —— 而且 Phase 1 和 Phase 3 用的是同一台機器，這一步實質上就是 Phase 3 的前置除錯。

**七個部分：**

1. 挑機型與開機
2. 磁碟與模型快取
3. 把 vLLM 跑起來
4. 正確性驗證 —— 對齊 MAE $39.85
5. 刻意失敗：`--max-lora-rank` 與兩種失敗模式
6. 原創實驗：runtime LoRA vs merged 權重
7. 四方對照表

---

## 精簡路線：Phase 1＋2 合併成一輪（本 lab 採用）

**這兩個 Phase 用同一台機器，時間主要花在「重啟 vLLM」**（每次都要載權重、編譯）。所以不按文件順序做，而是按「啟動」分組 —— 一次啟動把能做的事全做完，總共 5 次能起來的啟動 + 1 次預期失敗的。2026-10-18 實際開機約 3.5 小時（依 log 時間戳估，含發現 prefix cache 問題後重量的那段），約 $3。

| 啟動 | 設定（跟 ① 只差一個旗標） | 這次做完的事 | 出處 |
|---|---|---|---|
| ① | Part 3.1 的指令（runtime LoRA、`--max-model-len 512`、`pricer` + `pricer2`） | 量冷啟動時間、記 `KV cache size` / `Maximum concurrency` / `Model loading took` 的 log（`max_num_batched_tokens` 0.31 不印，見 Phase 2 Part 4.3）、MAE（`pricer` 與 `base`）、`/v1/models` 看到三個名字、飽和壓測、request-rate 掃描、瓶頸判讀 | 本文 Part 3–4；Phase 2 Part 2、6.1、7 |
| ② | `--max-model-len 131072`，**前景** | 只記錯誤訊息（預期起不來） | Phase 2 Part 3.1 第一步 |
| ③ | `--max-model-len 32768` | 飽和壓測一次，跟 ① 比 | Phase 2 Part 3.1 第二步 |
| ④ | `--max-num-batched-tokens 8192` | 飽和壓測 + 一個固定 rate，跟 ① 比 | Phase 2 Part 4.3 |
| — | （停 vLLM）merge LoRA | | 本文 Part 6.1 |
| ⑤ | merged bf16 | MAE + 飽和壓測 | 本文 Part 6；也是 Phase 2 Part 5 的 bf16 基準 |
| ⑥ | merged FP8 | MAE + 飽和壓測 + `Model loading took` | Phase 2 Part 5 |

📖 **讀就好、不實跑**：本文 Part 5.1–5.2（`--max-lora-rank` 太小 → 啟動失敗，已從 vLLM 原始碼確認；它的對照組「無聲失敗」在 ① 的 `--model base` 已經量了）、Phase 2 Part 4.1 / 4.2 / 4.4、Phase 2 Part 6.3（同一份 adapter 量 1 vs 2 個，結論太薄）。

**量測次數**：每組「熱身 1 次 + 正式 1 次」，只記正式那次（見 Phase 2 Part 1.2）。

---

## ⚠️ 開始前：先讀成本紀律

**這一階段每小時燒 $0.80。** 在做任何事之前，先把 [`91-cost-and-teardown.md`](91-cost-and-teardown.md) 讀過一遍，特別是「離桌檢查清單」。

三條鐵律：
1. **離開電腦前一定 `aws ec2 stop-instances`**（stop 不是 terminate，EBS 留著下次用）
2. **每天結束對一次帳**：`aws ce get-cost-and-usage`（指令在成本那份文件裡）
3. **不確定有沒有東西在跑就查**：`aws ec2 describe-instances --filters "Name=instance-state-name,Values=running"`

---

## Part 1：挑機型與開機

### 1.1 做法

先確認配額下來了：

```bash
aws service-quotas get-service-quota \
  --service-code ec2 --quota-code L-DB2E81BA \
  --region us-east-1 --query 'Quota.Value'
```

找一個帶 NVIDIA 驅動的 AMI（省得自己裝驅動）：

```bash
aws ssm get-parameters --region us-east-1 \
  --names /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id \
  --query 'Parameters[0].Value' --output text
```

建 key pair 與安全群組（第一次才需要，之後重用）：

```bash
# key pair：私鑰只能在建立時下載這一次，存進 private/（已 gitignore）
aws ec2 create-key-pair --region us-east-1 --key-name pricer-lab \
  --key-type ed25519 --query KeyMaterial --output text > private/pricer-lab.pem
chmod 400 private/pricer-lab.pem      # 權限太寬的話 ssh 會拒絕使用

# 安全群組（建在 default VPC）：印出的 sg-xxx 就是下面的 <SG>
# 剛建好時什麼都不准進，開 22 的規則見 1.3
aws ec2 create-security-group --region us-east-1 \
  --group-name pricer-lab-sg --description "pricer lab: ssh from my IP only" \
  --query GroupId --output text
```

開機（`<AMI>` 換成上面查到的、`<KEYPAIR>` 是 `pricer-lab`、`<SG>` 是剛拿到的 `sg-xxx`）：

```bash
aws ec2 run-instances --region us-east-1 \
  --image-id <AMI> \
  --instance-type g6.xlarge \
  --key-name <KEYPAIR> \
  --security-group-ids <SG> \
  --block-device-mappings '[{
    "DeviceName":"/dev/sda1",
    "Ebs":{"VolumeSize":150,"VolumeType":"gp3","DeleteOnTermination":true}
  }]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=pricer-lab},{Key=Project,Value=llm-inference-lab}]'
```

### 1.2 為什麼是 g6.xlarge

| 機型 | GPU | 記憶體 | 約略單價 | 判斷 |
|---|---|---|---|---|
| **g6.xlarge** | L4 (Ada) | 24GB | **$0.8048/hr** | ✅ **推薦**。支援 bf16 與 FP8，量化實驗做得全 |
| g5.xlarge | A10G (Ampere) | 24GB | 約 $1.0/hr | 可以，但比較貴且沒有 FP8 |
| g4dn.xlarge | T4 (Turing) | 16GB | 約 $0.53/hr | ⚠️ **沒有 bf16**，要加 `--dtype half`；部分量化 kernel 不支援。省下的錢會被多花的時間吃掉 |

我們的模型只有 3B（bf16 約 6.4GB），其實 16GB 就夠。**選 g6 不是為了跑得動，是為了 Phase 2 的量化實驗做得完整** —— L4 是 Ada 架構，有 FP8 硬體支援，這讓「bf16 / FP8 / AWQ / GPTQ」的對照表能真的做出四組數據。

**150GB 的 root EBS**：模型快取（bf16 權重 6.4GB + 各種量化版本）加上 vLLM 的 image（10GB+），100GB 會很緊。而且 Deep Learning AMI 本身就佔了約 50G（2026-10-18 實測 `df -h /`：146G 已用 50G）；跑完 Phase 1+2（image、權重、adapter、merged）後已用 88G。gp3 每月約 $12，但你只會用幾天。

### 1.3 安全群組

```bash
MY_IP=$(curl -s https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress --region us-east-1 \
  --group-id <SG> --protocol tcp --port 22 --cidr "${MY_IP}/32"
```

**只開 22，只開給你自己的 IP。** vLLM 的 port 走 SSH 通道，不要對外開放：

```bash
ssh -i <key> -L 8000:localhost:8000 ubuntu@<公有IP>
```

### ⚠️ 陷阱

- **`stop` 不是 `terminate`。** stop 之後 EBS 還在（每月約 $12），下次 start 模型快取還在，不用重下載。terminate 則是全部消失。
- **stop/start 後公有 IP 會變**（除非綁 Elastic IP，但閒置的 EIP 也要收費）。每次重連前先查新 IP。
- **你家的 IP 也會變**（家用網路、換地方、開手機熱點）。症狀是 `ssh` 卡住然後 `Operation timed out` —— 不是 `Permission denied`，因為封包在安全群組就被擋掉了。把新 IP 加進去，舊的順手刪掉：

  ```bash
  MY_IP=$(curl -s https://checkip.amazonaws.com)
  aws ec2 authorize-security-group-ingress --region us-east-1 \
    --group-id <SG> --protocol tcp --port 22 --cidr "${MY_IP}/32"

  # 看目前放行了哪些 IP，把舊的刪掉
  aws ec2 describe-security-groups --region us-east-1 --group-ids <SG> \
    --query 'SecurityGroups[0].IpPermissions[].IpRanges[].CidrIp'
  aws ec2 revoke-security-group-ingress --region us-east-1 \
    --group-id <SG> --protocol tcp --port 22 --cidr <舊IP>/32
  ```
- **spot 便宜 60–70% 但會被中斷。** 學習階段可以用，但要接受「跑到一半機器不見了」。模型快取在 EBS 上所以損失有限。
- 開機後**第一件事**就是設好關機提醒。真的。

---

## Part 2：磁碟與模型快取

### 2.1 做法

```bash
# SSH 進去之後
nvidia-smi                      # 確認看得到 L4
df -h /                         # 確認磁碟空間

# 把 HF cache 放在 EBS root volume 上（見下方說明，別放 instance store）
echo 'export HF_HOME=$HOME/hf-cache' >> ~/.bashrc
source ~/.bashrc
mkdir -p "$HF_HOME"

# 憑證
mkdir -p ~/private && chmod 700 ~/private
# 從本機複製上來：
#   scp -i <key> private/hf-token ubuntu@<IP>:~/private/hf-token
# 寫進 bashrc —— 只在當下 shell export 的話，隔天重連後就是空的
echo 'export HF_TOKEN=$(cat ~/private/hf-token)' >> ~/.bashrc
source ~/.bashrc

# vLLM 的編譯快取，理由見 Part 3.2
mkdir -p ~/vllm-cache
```

### 2.2 為什麼 HF cache 的位置很重要

g6.xlarge 有**兩種**磁碟，選錯的代價是每天重下載 6.4GB：

| 磁碟 | 路徑 | `stop` 之後 | 速度 |
|---|---|---|---|
| **EBS root volume**（我們掛的 150GB gp3） | `/`、`$HOME` | ✅ **保留** | 快 |
| **Instance store**（機型自帶的 250GB NVMe） | `/opt/dlami/nvme` 之類 | ❌ **全部消失** | 更快 |

⚠️ **Instance store 是跟實體主機綁在一起的。** 你 `stop` 再 `start`，AWS 會把你的機器放到另一台實體主機上 —— 那顆 NVMe 上的東西全沒了。

我們的流程是每天收工 `stop` 省錢，所以 **HF cache 必須放 EBS**（`$HOME/hf-cache`）。放 instance store 的話，每天早上都要重新下載 6.4GB 權重。

Instance store 適合放的是**真正的暫存**：壓測的原始輸出、編譯中間檔 —— 丟了也無所謂、而且要快的東西。

模型下載一次要幾分鐘。如果 cache 放在**會隨容器消失**的地方，你每次 `docker run` 都要重下載一次 —— 在按小時計費的機器上，這是純粹的浪費。

把 `HF_HOME` 指到 EBS 上的固定路徑，然後每次 `docker run` 都掛載它：

```bash
-v "$HF_HOME:/root/.cache/huggingface"
```

**這個習慣在 Phase 3 會變成 PVC 或 initContainer**，是同一個問題的不同層次解法。

### 2.3 驗證

```bash
nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv
# NVIDIA L4, 23034 MiB, 8.9
```

`compute_cap 8.9` = Ada 架構 = **支援 FP8**。這個數字決定 Phase 2 能做哪些量化實驗。

---

## Part 3：把 vLLM 跑起來

### 3.1 做法

先把 LoRA adapter 抓下來（vLLM 要絕對路徑，見陷阱）：

```bash
pip install --user huggingface_hub
python3 -c "
from huggingface_hub import snapshot_download
snapshot_download(
    'ed-donner/price-2025-11-28_18.47.07',
    revision='b19c8bfea3b6ff62237fbb0a8da9779fc12cefbd',
    local_dir='/home/ubuntu/adapter',
)
"
```

起 vLLM：

```bash
docker run --rm -d --name vllm \
  --gpus all \
  --ipc=host \
  -p 8000:8000 \
  -v "$HF_HOME:/root/.cache/huggingface" \
  -v "$HOME/vllm-cache:/root/.cache/vllm" \
  -v /home/ubuntu/adapter:/adapter:ro \
  -e "HF_TOKEN=$HF_TOKEN" \
  vllm/vllm-openai:latest \
    --model meta-llama/Llama-3.2-3B \
    --served-model-name base \
    --enable-lora \
    --lora-modules pricer=/adapter pricer2=/adapter \
    --max-lora-rank 256 \
    --max-loras 2 \
    --max-model-len 512 \
    --gpu-memory-utilization 0.85

docker logs -f vllm            # 等它說 "Application startup complete"
```

### 3.2 為什麼每個旗標都在那裡

| 旗標 | 為什麼 |
|---|---|
| `--ipc=host` | vLLM 用共享記憶體做行程間通訊。不設的話預設 `/dev/shm` 只有 64MB，**負載一上來就 crash**。（Phase 3 在 k8s 裡的對應是 `emptyDir: {medium: Memory}`） |
| `--gpus all` | 沒有這個容器裡看不到 GPU |
| `--max-lora-rank 256` | **我們的 adapter 是 r=256，預設值是 16。** 不設的話 vLLM 啟動時就會拒絕載入 adapter，見 Part 5 |
| `--max-model-len 512` | prompt 約 150 + 輸出 5。預設 131072 在 L4 上很可能連啟動都不行（一條 131072 token 序列的 KV cache 就要約 14 GiB，Phase 2 Part 3 實測）。設 512 也是一道防線：超長的請求直接被拒絕。**它對吞吐的影響其實不大** —— 這跟很多人的直覺相反，Phase 2 會量給你看 |
| `--gpu-memory-utilization 0.85` | 預先劃給權重 + KV cache 的比例。0.9 是預設，留一點餘裕比較不會在尖峰爆掉 |
| `-v $HOME/vllm-cache:/root/.cache/vllm` | vLLM 啟動時會做 torch.compile 與 CUDA graph 編譯，結果快取在容器的 `~/.cache/vllm`。容器是 `--rm` 起的，不掛出來的話**每次重啟都要重編** —— Phase 2 每組實驗都要重啟，累積起來很可觀。（快取路徑依 vLLM 版本而異，跑過一次後 `ls ~/vllm-cache` 確認有東西寫進去） |
| `--served-model-name base` | 讓 base model 和 LoRA 有不同的名字，這樣同一個服務可以兩個都打 —— Part 4 的 base 對照組要用 |
| `pricer2=/adapter --max-loras 2` | 同一份 adapter 掛成第二個名字，順便驗證多 LoRA 熱插拔（Phase 2 Part 6），省一次重啟。代價是多佔一份 r=256 的 LoRA 槽位、KV cache 稍小 —— ①③④ 都用這組旗標，彼此仍可比 |

**啟動 log 裡最該看的那兩行**：

```bash
docker logs vllm 2>&1 | grep -E "KV cache size|Maximum concurrency|Model loading took"
```

```
Model loading took 7.76 GiB memory and 61.423656 seconds
GPU KV cache size: 84,864 tokens, Maximum concurrency for 512 tokens per request: 165.75x
```

（2026-10-18 實測，vLLM 0.31.0，就是上面這組指令。舊版 vLLM 印的是 `# GPU blocks: N`，現在的版本已經不印這行了。`max_num_batched_tokens` 0.31 的 log 也不印，見 Phase 2 Part 4.3。）

- **`Model loading took`**：權重（加上 LoRA 槽位）佔的顯存。
- **`GPU KV cache size`**：KV cache 總共放得下多少 token。由「`--gpu-memory-utilization` 劃出的記憶體 − 權重 − 其他開銷」決定，**跟 `--max-model-len` 幾乎無關**。
- **`Maximum concurrency`**：「如果每個請求都長到 `--max-model-len`，能同時跑幾個」的**最壞情況估計**。vLLM 的 KV cache 是**按需分配**的（PagedAttention），實際能跑多少並發，看的是請求真正用了多少 token，不是這個數字。

**Phase 2 調校時，這兩行是你的儀表板。**

### 3.3 驗證

```bash
curl -s localhost:8000/v1/models | python3 -m json.tool
# 應該看到 "base"、"pricer"、"pricer2" 三個

curl -s localhost:8000/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "pricer",
    "prompt": "What does this cost to the nearest dollar?\n\nTitle: Bosch 12V Max Cordless Drill\nCategory: Tools\nBrand: Bosch\nDescription: A compact cordless drill for home use.\nDetails: Includes two batteries and a charger.\n\nPrice is $",
    "max_tokens": 5,
    "temperature": 0
  }' | python3 -c "import json,sys; print(repr(json.load(sys.stdin)['choices'][0]['text']))"
```

### ⚠️ 陷阱

- **`--lora-modules` 要用容器內的路徑**（`/adapter`，不是主機上的 `/home/ubuntu/adapter`）。路徑錯的話 vLLM 啟動時就會報 `LoRAAdapterNotFoundError` 並退出 —— 用 `-d --rm` 背景跑的話，容器一退出就被刪掉，`docker logs` 什麼都拿不到。**第一次跑新設定時先用前景模式**（不加 `-d`）。
- **必須用 `/v1/completions`，不是 `/v1/chat/completions`。** base model 沒有 chat template，套 chat 格式的話 prompt 就不是訓練時的樣子。
- **回傳只有「續寫」部分，不含 prompt。** 課程 Modal 版的 `result.split("Price is $")[1]` 照抄會 IndexError —— 那是因為 HF 的 `generate()` 會把 prompt 一起回傳，vLLM 不會。
- **gated model 需要 `HF_TOKEN`**，而且要接受過授權。403 就是這個問題。隔天重連後出現 401，先 `echo ${#HF_TOKEN}` 看是不是 0（Part 2 有把它寫進 `~/.bashrc`）。
- **第一次啟動要下載 6.4GB 的權重**，加上 torch.compile / CUDA graph 編譯，總共 2–5 分鐘。這個時間就是 Phase 3 `startupProbe` 的依據 —— **現在就量一下並記下來**。⚠️ 要量的是**沒有編譯快取**的那一次：k8s 裡新排到節點的 pod 不會有你的 `~/vllm-cache`。也要**先 `docker pull vllm/vllm-openai:latest`** 再計時，不然量到的是下載 10GB+ image 的時間（k8s 裡拉 image 也不算在 `startupProbe` 裡）。實測（2026-10-18）：冷啟動 249 秒；權重與編譯快取都在時 121 秒。
- **`/health` 回 200 不等於壓測可以開始。** 壓測一定要指定 `--seed`、熱身與正式用不同的值，不然第二次會命中 prefix cache，吞吐虛高好幾倍（Phase 2 Part 2.1）。

---

## Part 4：正確性驗證 —— 對齊 MAE $39.85

**這是本階段的核心。** 沒有這一步，後面所有效能數字都沒有意義。

### 4.1 做法

在**本機**跑（透過 SSH 通道）：

```bash
# 本機另一個終端
ssh -i <key> -N -L 8000:localhost:8000 ubuntu@<IP>

# 再另一個終端
cd ~/Desktop/side-projects/llm-inference-k8s-lab
uv run --with datasets eval/measure.py \
  --base-url http://localhost:8000 \
  --model pricer \
  --size 200 \
  --label "phase1-vllm-bf16-lora-rank256"
```

### 4.2 判讀

| MAE | 代表什麼 | 下一步 |
|---|---|---|
| **$36 ~ $45** | ✅ 正確。LoRA 完整生效 | 記進 `RESULTS.md`，往 Part 5 走 |
| **約 $200** | ❌ LoRA 完全沒生效 | 請求打到 base 了：檢查 `--model` 有沒有打成 `pricer`（adapter 路徑錯的話 vLLM 根本起不來，不會走到這裡） |
| **$50 ~ $80** | ⚠️ 介於中間 | 先確認跟 Phase 0 用的是同一份評估（`eval/measure.py`、`--size 200`、`temperature 0`），再跟 Phase 0 的 $40.79 對照 |

**同時記下 base model 的分數當對照組**：

```bash
uv run --with datasets eval/measure.py \
  --base-url http://localhost:8000 \
  --model base \
  --size 200 \
  --label "phase1-vllm-bf16-BASE-no-lora"
```

2026-10-18 實測：**$200.29**（R² −695.9%）。**不是** leaderboard 上「未微調的 Llama 3.2」的 $110.72 —— 那是課程的 evaluator 量的，跟本 repo 的 `eval/measure.py` 不可直接比。`parse_price` 解析是對的（`'1,999.99'` → 1999.99）：base 遇到不熟的商品就猜 1,999.99、1,499.99 這類零售價，誤差最大的 8 筆就占總誤差 30%。**有了這個對照組，你就永遠分得清「LoRA 沒生效」和「LoRA 效果不好」。**

### 4.3 跟 Phase 0 的差異怎麼解釋

你現在有兩個數字：

| 環境 | 量化 | LoRA |
|---|---|---|
| Phase 0（Mac） | base Q8_0（llama.cpp） | GGUF f16 |
| Phase 1（GPU） | base **bf16**（無量化） | 原生 safetensors |

**Phase 1 的 MAE 應該略優於 Phase 0**，差距就是「Q8_0 量化的代價」。如果兩者幾乎一樣，代表 Q8_0 對這個任務幾乎無損 —— 那本身就是個有價值的結論（而且是你量出來的）。

**實測（2026-10-18）是後者**：Phase 1 $40.74、Phase 0 $40.79，差 0.1% —— Q8_0 對這個任務幾乎無損。

**還有一個共同的變因值得記在文件裡**：這個 adapter 是用 **QLoRA（4-bit NF4 base）** 訓練的，但兩個環境都是在更高精度的 base 上推論。訓練與推論的量化不一致會造成漂移，這是 QLoRA 的已知特性，不是你的 bug。

---

## Part 5：刻意失敗 —— `--max-lora-rank` 與兩種失敗模式（📖 精簡路線：5.1–5.2 讀就好，5.3 要懂）

**精簡路線不實跑 5.1–5.2**：依 vLLM 原始碼（見 5.2 的註），rank 超過上限會在啟動時就丟錯退出，行為明確，跑一次只是多一次重啟。**5.3 的「無聲失敗」對照組已經在 Part 4 的 `--model base` 量過了**，那才是這一節的重點。

它示範的是「**大聲失敗**」—— 真正危險的是它的反面，這一節要把兩種並排看清楚。

### 5.1 做法：不帶 `--max-lora-rank` 啟動

```bash
docker stop vllm

# 前景模式（不加 -d）：它應該起不來，錯誤要直接印在終端上
docker run --rm --name vllm \
  --gpus all --ipc=host -p 8000:8000 \
  -v "$HF_HOME:/root/.cache/huggingface" \
  -v "$HOME/vllm-cache:/root/.cache/vllm" \
  -v /home/ubuntu/adapter:/adapter:ro \
  -e "HF_TOKEN=$HF_TOKEN" \
  vllm/vllm-openai:latest \
    --model meta-llama/Llama-3.2-3B \
    --served-model-name base \
    --enable-lora \
    --lora-modules pricer=/adapter \
    --max-model-len 512 \
    --gpu-memory-utilization 0.85
    # ← 注意：這次「沒有」--max-lora-rank，用預設值 16
```

### 5.2 你會觀察到什麼

base model 載完、要掛 LoRA 時直接報錯退出：

```
ValueError: LoRA rank 256 is greater than max_lora_rank 16.
```

> 這是依目前 vLLM 原始碼（`vllm/lora/peft_helper.py` 的 rank 檢查、`init_static_loras()` 載入失敗就中止啟動）推得的預期。確切訊息與時間點依版本而異 —— **以你實際看到的為準，貼進 `RESULTS.md`**。舊版 vLLM 的行為可能不同。

**服務根本不會起來。** vLLM 啟動時會驗證每個 `--lora-modules`：rank、路徑，還有它不支援的設定（DoRA、adapter bias 等）。任何一個不過就中止。

這跟 Phase 0 的 initContainer「找不到 adapter 就不准啟動」是同一個設計哲學：**fail fast**。

### 5.3 真正危險的是另一種

| | **大聲失敗** | **無聲失敗** |
|---|---|---|
| 例子 | `--max-lora-rank` 太小、adapter 路徑錯 | 請求的 `model` 打到 `base` 而不是 `pricer`（例如 pricer-api 的 `MODEL_NAME` 設錯） |
| 服務起得來嗎 | ❌ | ✅ |
| health check / readiness | ❌ | ✅ 全綠 |
| API | — | ✅ HTTP 200，回傳合法的價格 |
| MAE | — | ❌ 約 $200（實測 $200.29） |
| 誰抓得到 | 任何一個 probe | **只有業務層面的正確性驗證** |

無聲失敗你在 Part 4 其實已經做過了 —— 就是那個 `--model base` 的對照組。**把兩邊並排記進 `RESULTS.md`。**

**這證明了一件事：服務起來 ≠ 正確。** 所有的健康檢查、readiness probe、`/v1/models` 回應、HTTP 200 都是綠的，只有 MAE 會告訴你答案錯了。

**面試答案的形狀**：
> 「我的 adapter 是 r=256，vLLM 的 `--max-lora-rank` 預設 16。不設的話 vLLM 啟動時就會拒絕 —— 這是好的設計，大聲的錯誤很好處理。真正危險的是反過來的情況：上游把請求的 model 名稱設成 base，服務完全正常、health check 全綠、回傳合法的價格，但 MAE 從 $40 變成 $200。所以我在部署流程裡加了一道 MAE 門檻 —— health check 只抓得到大聲的錯誤。」

### 5.4 這件事的通用啟示

**每個推論服務的部署流程都該有一道「業務正確性」的關卡**，而不只是 liveness / readiness。對這個 lab 來說就是：

```
部署 → health check 通過 → 跑 50 筆的 MAE smoke test → MAE < 門檻才放流量
```

這正是 Phase 3 可以用 k8s Job 實作的東西（在 rollout 之後跑一個評估 Job，失敗就 `rollout undo`）。

**記得把 `--max-lora-rank 256` 加回去再往下走。**

---

## Part 6：原創實驗 —— runtime LoRA vs merged 權重

**目的**：r=256 是很大的 rank，runtime LoRA 的 kernel 開銷不小。這是一個真實的工程取捨，而且沒有人幫你量過。

### 6.1 做法

先把 LoRA merge 進 base。**在 vLLM 的 image 裡做**，那裡已經有 torch 和 transformers，只要補裝 peft：

```bash
# 先停掉 vLLM：merge 在 CPU 上做，會吃掉十幾 GB 的「主機」記憶體，
# 而 g6.xlarge 只有 16 GiB。vLLM 還開著的話兩邊加起來可能把主機擠到 OOM。
docker stop vllm

cat > ~/merge.py <<'EOF'
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel

base = AutoModelForCausalLM.from_pretrained(
    "meta-llama/Llama-3.2-3B", torch_dtype=torch.bfloat16
)
merged = PeftModel.from_pretrained(base, "/work/adapter").merge_and_unload()
merged.save_pretrained("/work/merged")
AutoTokenizer.from_pretrained("meta-llama/Llama-3.2-3B").save_pretrained("/work/merged")
print("merge 完成")
EOF

docker run --rm \
  -v "$HF_HOME:/root/.cache/huggingface" \
  -v /home/ubuntu:/work \
  -e "HF_TOKEN=$HF_TOKEN" \
  --entrypoint bash \
  vllm/vllm-openai:latest \
  -c "pip install -q peft && python3 /work/merge.py"

ls -lh ~/merged
```

**為什麼不在主機上 `pip install torch transformers peft`**：torch 加上 CUDA 函式庫要下載好幾 GB、裝十幾分鐘，在按小時計費的機器上是純浪費。vLLM 的 image 本來就有一套對得上版本的 torch。

起一個 merged 版本的服務：

```bash
docker run --rm -d --name vllm \
  --gpus all --ipc=host -p 8000:8000 \
  -v "$HOME/vllm-cache:/root/.cache/vllm" \
  -v /home/ubuntu/merged:/merged:ro \
  vllm/vllm-openai:latest \
    --model /merged \
    --served-model-name pricer \
    --max-model-len 512 \
    --gpu-memory-utilization 0.85
```

兩邊都跑一次評估，**同時記 MAE 和吞吐**：

- **MAE**：照 Part 4，從本機跑 `eval/measure.py`
- **吞吐**：在 GPU 機器上跑 `vllm bench serve`（做法見 Phase 2 Part 2.1）

```bash
# 熱身 seed 1 + 正式 seed 2，只記第二次（seed 不同才不會命中 prefix cache）
for s in 1 2; do
  docker exec vllm vllm bench serve --backend openai --base-url http://localhost:8000 \
    --model pricer --tokenizer /merged --dataset-name random \
    --random-input-len 150 --random-output-len 5 --num-prompts 500 --request-rate inf \
    --seed $s --percentile-metrics ttft,tpot,e2el --metric-percentiles 50,95,99
done
```

runtime LoRA 那列**不用重跑**：直接用 ① 在 Phase 2 Part 2.1 量的飽和結果。在同一個 ① 上再跑一次 seed 1 / 2，會命中前一次留下的 prefix cache，吞吐虛高。

**為什麼吞吐不用本機的 `measure.py --workers 20` 量**：本機到 us-east-1 要繞半個地球，一來一回約 200ms，量到的主要是網路延遲，不是 GPU。`measure.py` 只用來算 MAE。

### 6.2 取捨在哪

| | **Runtime LoRA** | **Merged 權重** |
|---|---|---|
| 記憶體 | 一份 base + N 個小 adapter | 每個 adapter 一份完整模型 |
| 速度 | 每層多一次低秩運算，**r 越大越貴** | 跟原生模型完全一樣 |
| 熱插拔 | ✅ 可以，多租戶共用一個 base | ❌ 換模型要重載整個服務 |
| 部署複雜度 | 低（只推 adapter；本 lab 的 r=256 是 1.5GB，大小跟 rank 成正比，一般 r=8~32 約幾十到兩百 MB） | 高（每次都要 merge + 推一份完整權重） |
| 適用 | 多 adapter、流量分散、需要彈性 | 單一 adapter、追求極致延遲 |

**r=256 讓這個取捨變得不明顯** —— 一般 r=8~32 的 adapter，runtime 的開銷小到可以忽略；r=256 就未必了。**這就是為什麼要量。**

### 6.3 驗證

成功條件：**兩邊的 MAE 應該幾乎相同**（差異在小數點後），因為數學上是等價的。如果差很多，代表 merge 或載入有問題。

⚠️ **精簡路線的已知混淆變因**：runtime LoRA 那組（合併路線 ①）多帶了 `pricer2 --max-loras 2`，多佔一份 r=256 的 LoRA 槽位、KV cache 稍小。對本 workload（KV cache 用量很低）影響應該不大，但吞吐差距不能 100% 歸給「runtime LoRA vs merged」—— `RESULTS.md` 那一列要註明。

吞吐則可能有明顯差異 —— 那就是你的實驗結果。填進 `RESULTS.md`。

**實測（2026-10-18）**：

| | runtime LoRA（r=256、`--max-loras 2`） | merged |
|---|---|---|
| MAE | $40.74 | $40.80 |
| `Model loading took` | 7.76 GiB | 6.04 GiB |
| KV cache size | 84,864 | 106,400 |
| 飽和吞吐 | 35.77 req/s | **53.38 req/s（+49%）** |
| p95 TTFT | 13,341 ms | 8,966 ms |

LoRA 槽位佔了約 1.7 GiB；飽和時 KV 使用率只有 13%，所以 KV 少 20% 應該不是主因 —— 推測差距主要來自每層多出的 r=256 低秩運算（prefill 是 compute-bound）；但 runtime 那組多了 `--max-loras 2`，兩者沒有拆開量。

### ⚠️ 陷阱

- **merge 出來的模型約 6GB（實測 `model.safetensors` 6.0G，CPU merge 花 1 分 48 秒），磁碟要夠。**
- **merge 在容器裡以 root 跑，`~/merged` 的檔案擁有者是 root、`model.safetensors` 權限 600。** vLLM 容器也是 root 所以讀得到，但之後以 ubuntu 身分 `scp` 或搬進 PVC 會 `Permission denied` → 先 `sudo chown -R ubuntu:ubuntu ~/merged`。
- **merge 要用跟訓練時相同的 dtype 概念思考**：adapter 是 QLoRA（4-bit base）訓的，merge 到 bf16 base 上會有些微漂移。這跟 Part 4 講的是同一件事。
- **merged 版本沒有 `--enable-lora`**，所以 `--max-lora-rank` 那個坑自然消失 —— 這本身就是 merged 的一個優點。

---

## Part 7：四方對照表

把到目前為止的所有數字整理成一張表填進 `RESULTS.md`：

| # | 環境 | 權重精度 | LoRA 方式 | MAE | 備註 |
|---|---|---|---|---|---|
| 0 | leaderboard 基準 | — | — | **$39.85** | 課程公開數字，驗收門檻 |
| 1 | leaderboard（未微調） | 4-bit | 無 | $110.72 | 課程 evaluator，跟 #6 不可直接比 |
| 2 | Mac / llama.cpp | Q8_0 | GGUF f16 runtime | $40.79 | Phase 0 |
| 3 | GPU / vLLM | bf16 | runtime, rank 256 | $40.74 | Phase 1 主線 |
| 4 | GPU / vLLM | bf16 | runtime, **預設 rank 16** | 起不來 | 大聲失敗（精簡路線未實跑，依原始碼推論） |
| 5 | GPU / vLLM | bf16 | **merged** | $40.80 | 與 #3 幾乎相同 ✅ |
| 6 | GPU / vLLM | bf16 | 無（base） | $200.29 | **無聲失敗**的樣子；本 eval 的「LoRA 沒生效」指紋 |

**這張表就是你的面試素材。** 它證明的不是「我會部署 vLLM」，而是「我會設計對照實驗、我知道每個數字為什麼是那樣」。

---

## Phase 1 驗收清單

- [x] `nvidia-smi` 看得到 L4，`compute_cap` 是 8.9
- [x] vLLM 的 `/v1/models` 同時列出 `base`、`pricer`、`pricer2`
- [x] 主線 MAE 落在 $36–$45
- [x] base model 的對照組 MAE 約 $200（遠高於主線，無聲失敗看得出來）
- [ ] （精簡路線跳過）預設 `--max-lora-rank` 的啟動錯誤訊息記錄了，並跟 base 對照組（無聲失敗）並排
- [x] merged vs runtime LoRA 的 MAE 幾乎相同（驗證 merge 正確）
- [x] 已量並記下 vLLM 的**冷啟動時間**（Phase 3 的 `startupProbe` 要用）
- [x] 啟動 log 的 `KV cache size`、`Maximum concurrency` 已記下，`max_num_batched_tokens` 從原始碼確認（Phase 2 調校的基準）
- [x] 對照表七行都填完（#4 依原始碼推論）
- [x] **機器已 `stop`**

---

## 收工（每次離開電腦都要做）

```bash
aws ec2 stop-instances --region us-east-1 --instance-ids <ID>

# 確認真的停了
aws ec2 describe-instances --region us-east-1 \
  --filters "Name=instance-state-name,Values=running,pending" \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,State.Name]' --output table
```

**空的表格 = 安全。** 有東西 = 立刻停掉。

---

## 下一站

→ [`05-phase2-tuning.md`](05-phase2-tuning.md)：從「跑得起來」進到「知道瓶頸在哪」。壓測、調校、量化對照表 —— 這是面試被追問時的分水嶺。
