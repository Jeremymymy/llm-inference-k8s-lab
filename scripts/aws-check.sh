#!/usr/bin/env bash
# 一鍵健檢（docs/91-cost-and-teardown.md）：全部「（無）」= 今天不會被收錢。
# 有資源時，每一筆底下會印出對應的清理指令（只印、不執行）。
# 用法：scripts/aws-check.sh [region]   預設 us-east-1
# 結束碼：0 = 全空，1 = 憑證或查詢失敗，2 = 有資源在收費
set -uo pipefail
R=${1:-us-east-1}
found=0

# 憑證失效時每一段都會報錯、看起來像「全空」，先擋掉
aws sts get-caller-identity --query Account --output text >/dev/null || {
  echo "AWS 憑證無效，先登入再跑" >&2; exit 1; }

# section <標題> <清理指令，{} 會換成該列第一欄> <aws 查詢...>
section() {
  local title=$1 fix=$2 out id rest; shift 2
  echo "=== $title ==="
  out=$("$@" --region "$R" --output text) || { echo "（查詢失敗，見上方錯誤）"; found=1; return; }
  [ -z "$out" ] && { echo "（無）"; return; }
  [ "$found" -eq 0 ] && found=2
  while IFS=$'\t' read -r id rest; do
    echo "  $id  $rest"
    echo "    → ${fix//\{\}/$id}"
  done <<<"$out"
}

section "執行中的機器" "aws ec2 stop-instances --region $R --instance-ids {}" \
  aws ec2 describe-instances --filters "Name=instance-state-name,Values=running,pending" \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,State.Name]'
section "閒置的 EBS（確認不要了再刪）" "aws ec2 delete-volume --region $R --volume-id {}" \
  aws ec2 describe-volumes --filters "Name=status,Values=available" \
  --query 'Volumes[].[VolumeId,Size]'
section "未綁定的 EIP" "aws ec2 release-address --region $R --allocation-id {}" \
  aws ec2 describe-addresses --query 'Addresses[?AssociationId==null].[AllocationId,PublicIp]'
section "NAT Gateway" "aws ec2 delete-nat-gateway --region $R --nat-gateway-id {}" \
  aws ec2 describe-nat-gateways --filter "Name=state,Values=available" \
  --query 'NatGateways[].[NatGatewayId]'
section "Load Balancer" "aws elbv2 delete-load-balancer --region $R --load-balancer-arn {}" \
  aws elbv2 describe-load-balancers --query 'LoadBalancers[].[LoadBalancerArn,LoadBalancerName,Type]'

echo
case $found in
  0) echo "✅ 全空，今天不會被收錢" ;;
  2) echo "⚠️  有資源在收費，照上面 → 的指令清理（Phase 4 的 NAT / LB 照 docs/07 拆）" ;;
  *) echo "❌ 有查詢失敗，結果不可信" ;;
esac
exit "$found"
