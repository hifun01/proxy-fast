# proxy-fast

一条命令创建阿里云 ECS 抢占式代理实例，部署精简的 sing-box，并返回 Mihomo / sing-box 的订阅地址。

```bash
proxy-fast on
proxy-fast status
proxy-fast off
```

`on` 创建实例并等到代理出口和公网订阅检查通过后输出地址；再次执行会复用现有实例。`status` 查询云端真实状态并检查订阅。`off` 释放本工具拥有的实例、安全组、交换机和 VPC，确认资源消失后才清空本地资源记录。

## 本地安装

控制端支持 Windows、Linux、macOS，需要 Python 3.10+。运行时只使用 Python 标准库，不需要阿里云 SDK、SSH、Docker 或 aliyun CLI。

在仓库目录安装命令：

```bash
python -m pip install -e .
proxy-fast --version
```

也可以完全不安装，直接 `python proxy-fast on`（Windows 同样适用）。以下文档中的 `proxy-fast` 均可替换为 `python proxy-fast`。

### 仅首次准备凭证

如果本机已配置 aliyun CLI 的 AK / StsToken profile，工具自动读取 `~/.aliyun/config.json` 中的当前 profile，也支持 `--profile 名称`。环境变量凭证优先于 profile。

没有现成 profile 时，在**控制端**设置环境变量：

Linux / macOS：

```bash
export ALIBABA_CLOUD_ACCESS_KEY_ID='你的 AccessKey ID'
export ALIBABA_CLOUD_ACCESS_KEY_SECRET='你的 AccessKey Secret'
# 使用临时 STS 凭证时还需要：
# export ALIBABA_CLOUD_SECURITY_TOKEN='你的 STS Token'
```

Windows PowerShell：

```powershell
$env:ALIBABA_CLOUD_ACCESS_KEY_ID = '你的 AccessKey ID'
$env:ALIBABA_CLOUD_ACCESS_KEY_SECRET = '你的 AccessKey Secret'
# $env:ALIBABA_CLOUD_SECURITY_TOKEN = '你的 STS Token'
```

使用有相应 ECS / VPC 权限的 RAM 身份；下方「RAM 权限策略」中的 JSON 是按所用操作整理的策略起点，资源范围为 `*`，应按你的账号管理要求授权。工具会用 STS GetCallerIdentity 识别账号。AccessKey 不会写入实例 UserData、订阅或本地资源状态。

### 开始使用

```bash
proxy-fast on
```

输出示例：

```text
Mihomo:   http://公网IP:8080/随机令牌/clmi.yaml
sing-box: http://公网IP:8080/随机令牌/sbox.json
```

把对应地址手动添加到客户端的远程配置 / 订阅中。进度写入 stderr，地址写入 stdout。`clmi.yaml` 使用 JSON 格式表示 YAML 配置，Mihomo 可以直接读取。sing-box 配置包含 TUN 与本机 `127.0.0.1:7890` mixed 入站，TUN 需要客户端相应权限。

```bash
proxy-fast status
proxy-fast off
```

**停止使用后执行 `off`。本工具不设置自动过期，也不自动重建被回收的实例。** 抢占式实例可能被阿里云回收，恢复节点时重新执行 `on`。回收后 VPC 等自建网络仍由 `off` 负责清理。

## 统一配置文件

直接编辑仓库根目录的 **`proxy-fast.ini`**，保存后运行 `proxy-fast on`，不必每次传参数。文件内有逐项中文注释，主要配置如下：

```ini
[cloud]
region = eu-west-2
profile =

[instance]
price_limit = 0.10
protection_hours = 1
zone =
instance_type =
image_id =

[disk]
size_gib = 20
category = auto

[network]
bandwidth_mbps = 100

[controller]
timeout_seconds = 600
state_dir =
```

每一项的填写示例（示例不表示当前库存或价格推荐）：

| 配置项 | 填写示例 | 含义与限制 |
| --- | --- | --- |
| `[cloud] region` | `region = eu-west-2`；`region = ap-northeast-1` | 巴黎；东京。 |
| `[cloud] profile` | `profile = default`；`profile = spot` | 必须是本机已配置的阿里云 CLI profile；留空使用当前 profile / 环境变量凭证。AccessKey 不写进配置文件。 |
| `[instance] price_limit` | `price_limit = 0.20` | 计算资源每小时最高出价 0.20，按账号结算币种；磁盘和流量另计，不是总账单上限。 |
| `[instance] protection_hours` | `protection_hours = 0`；`protection_hours = 1` | 无保护期；1 小时保护期。不是自动到期时间。 |
| `[instance] zone` | `zone = eu-west-2a` | 属于所选地区的可用区，需确认当前支持和库存；留空自动选择。 |
| `[instance] instance_type` | `instance_type = ecs.e-c1m1.large` | 需符合 X86、1–2 vCPU / 1–4 GiB 筛选范围，并支持该地区的抢占式库存；留空自动选择。 |
| `[instance] image_id` | `image_id = ubuntu_24_04_x64_20G_alibase_20240927.vhd` | **仅为 ID 格式示例，不保证有效**。实际需从当前地区的官方 Ubuntu 24.04/22.04 X86 cloud-init 镜像列表复制 ImageId；留空自动选择。 |
| `[disk] size_gib` | `size_gib = 20`；`size_gib = 40` | 20 / 40 GiB，允许 20–2048；镜像要求更大容量时会自动提高。 |
| `[disk] category` | `category = auto`；`category = cloud_essd`；`category = cloud_ssd`；`category = cloud_efficiency` | 自动 / ESSD / SSD / 高效云盘；指定类型缺货时不会换成另一种盘。 |
| `[network] bandwidth_mbps` | `bandwidth_mbps = 20`；`bandwidth_mbps = 100` | 出口带宽上限 20 / 100 Mbps，允许 1–100。按实际公网流量计费，**没有 GB 流量或总费用上限**。 |
| `[controller] timeout_seconds` | `timeout_seconds = 600`；`timeout_seconds = 900` | 等待时限 10 / 15 分钟，允许 30–3600 秒；超时不会自动释放实例。 |
| `[controller] state_dir` | `state_dir = ./state/node-a`；`state_dir = D:\proxy-fast-state\node-a`；`state_dir = /home/admin/.local/state/proxy-fast-node-a` | 相对路径 / Windows 绝对路径 / Linux、macOS 绝对路径；不要加引号。相对路径以配置文件目录为基准，留空使用用户默认目录；已有资源时保持原状态目录。 |

默认依次查找：当前工作目录的 `proxy-fast.ini`、源码/可编辑安装对应仓库中的 `proxy-fast.ini`。普通 wheel 安装后，请把配置文件放在工作目录，或显式指定绝对路径：

```bash
proxy-fast on --config ./proxy-fast.ini
proxy-fast status --config ./proxy-fast.ini
proxy-fast off --config ./proxy-fast.ini
```

也支持 `proxy-fast --config ./proxy-fast.ini on`，或通过 `PROXY_FAST_CONFIG` 指定配置路径。显式指定的文件不存在、配置项拼错或格式错误时会报错，不会静默使用默认值。

覆盖优先级：**命令行参数 > 环境变量 `PROXY_FAST_REGION` / `PROXY_FAST_PRICE_LIMIT` > 配置文件 > 内置默认值**。例如临时限制带宽：

```bash
proxy-fast on --bandwidth 20
proxy-fast on --disk-size 40 --disk-category cloud_essd
```

配置只用于新建实例，修改文件不会调整已有实例；要换规格、出价、带宽或磁盘，先使用原状态目录执行 `off`，再执行 `on`。`off` / `status` 使用状态中保存的地区，避免配置改动后找错资源。

## 默认创建什么

| 项目 | 默认值 |
| --- | --- |
| 地域 | 巴黎 `eu-west-2` |
| 实例 | 有库存的 X86 抢占式，1–2 vCPU / 1–4 GiB；按 CPU、内存排序选择，**不是实时价格最低排序** |
| 竞价方式 | `SpotWithPriceLimit`，计算资源最高出价 **0.10 / 小时，单位按账号结算币种** |
| 保护期 | 1 小时；保护期后可能被回收，不代表自动到期 |
| 镜像 | 阿里云官方 Ubuntu 24.04，找不到则选择 22.04 |
| 系统盘 | 查询规格兼容及库存，优先 ESSD，其次 SSD / 高效云盘；至少 20 GiB，随实例释放；不创建数据盘、快照 |
| 公网 | 自动分配 IPv4，按流量计费，出口带宽上限 100 Mbit/s；不创建 EIP / NAT 网关 |
| 网络 | 独立 VPC / 交换机 / 普通安全组，带本地 owner 标记 |
| 入站 | 仅增加 TCP 443（节点）及 TCP 8080（订阅），不开放 SSH |
| 出站 | 安全组允许全部出站，用于 DNS、下载、REALITY 握手和代理流量 |
| 节点 | 官方 sing-box 1.14.4，固定 SHA256；VLESS + REALITY + Vision，握手域名 `apple.com` |

出价是**计算资源的上限**，不是总账单预算，也不是实际成交价。系统盘、实际公网流量另计；100 Mbit/s 是带宽上限。账号需要开通相应地域/产品并有余额及抢占式配额。出价太低、配额不足或规格与磁盘不兼容时可能创建失败；不会退回创建普通按量实例。失败后会保留资源记录，请执行 `off` 清理或调整参数重试。

首次控制地域、价格等参数：

```bash
proxy-fast on --region eu-west-2 --price-limit 0.10
# 可选：指定可用区、规格；只允许库存筛选内的 X86 小规格
proxy-fast on --zone eu-west-2a --instance-type ecs.e-c1m1.large
# 无保护期模式（是否供应以地域/规格为准）
proxy-fast on --protection 0
# 脚本等待时限，超时不会自动删除实例
proxy-fast on --timeout 900
```

以上参数均可改在 `proxy-fast.ini` 中设置。已有实例时 `on` 复用实例，不修改规格、出价或带宽；要换参数，先 `off` 再 `on`。切换地域前也必须先 `off`。

订阅通过 **HTTP + 随机路径令牌**提供，HTTP 不加密；持有链接的人可以取得节点凭证。工具不申请域名/证书，不安装面板、订阅转换器或额外代理协议。

## 状态与恢复

控制端每账号默认管理一套资源。状态保存在：

- Linux / macOS：`~/.local/state/proxy-fast/<账号ID>/state.json`（支持 `XDG_STATE_HOME`）。
- Windows：`%LOCALAPPDATA%\proxy-fast\<账号ID>\state.json`。

状态文件包含资源 ID、订阅令牌和幂等请求，用于失败重试与清理。Linux/macOS 目录权限 700、文件 600；Windows 依赖用户目录访问权限。**资源尚未释放时不要删除状态文件。** 命令使用进程锁，同一状态目录不允许并发修改。

```bash
proxy-fast status --json
proxy-fast on --json
proxy-fast off --json
```

`status` 只在实时检查通过时返回订阅。`reclaimed` 表示已记录实例消失；`provisioning` 表示实例运行但尚未就绪；`unhealthy` 表示曾经就绪的节点当前未通过检查；`creation-uncertain` 表示创建请求响应不确定；`incomplete` / `cleanup-pending` 表示还有未完成的部署 / 清理。`status` 返回成功退出码表示查询完成，应读取 JSON 的 `status` 判断可用性。

创建请求在发送前会持久化 ClientToken。遇到网络错误，重新运行同一命令；工具先查找带 owner 标记的实例，再用原请求恢复，避免重复创建。`off` 遇到未确认结果的创建请求，也会以原 ClientToken 确认结果后删除；极端情况下，原请求从未到达云端，这一步可能短暂创建再释放实例。若确认过程仍失败，则保留状态并报错，不会宣称已清理完成。

删除前同时核对本地记录 ID 与云端 owner / role 标记。`off` 不强制删除被其他资源占用的 VPC，也不管理没有相符标记的资源。如果你手动移除标记或把其他资源放进该 VPC，清理可能需要先在控制台处理这些改动。

管理多个相互独立的节点时，给每组命令使用各自的状态目录：

```bash
proxy-fast on --state-dir ./node-a --region eu-west-2
proxy-fast on --state-dir ./node-b --region ap-northeast-1
proxy-fast status --state-dir ./node-a
proxy-fast off --state-dir ./node-a
proxy-fast off --state-dir ./node-b
```

不要让多台控制端复制同一份状态后同时运行；本地锁不提供跨机器协调。

## 排错与单机部署

部署超时后，实例仍可能继续运行并计费。先执行 `status`，必要时重新执行 `on` 等待；不用了就 `off`。初次部署总耗时包含 ECS 调度、系统启动、cloud-init 和 GitHub 下载，之前测试的数秒仅是系统已启动后的节点安装耗时。

通过 ECS 控制台/云助手查看：

```bash
cat /var/log/proxy-fast/bootstrap.log
journalctl -u proxy-fast -u proxy-fast-sub -n 50 --no-pager
```

实例内文件：`/etc/proxy-fast/state.json`（私有凭证）、`/etc/proxy-fast/result.json`、`/usr/local/lib/proxy-fast/`、`/var/lib/proxy-fast/public/`。控制端下载 `ready.json` 验证本次部署标识、出口自检结果、两种公网订阅及 TCP 443 可达；它不是定期测速或完整客户端巡检。

原先的单机入口保留，可在现成 Ubuntu / Debian systemd VPS 上使用：

```bash
sudo bash proxy-fast.sh
sudo bash proxy-fast.sh status
```

单机脚本支持 X86_64 / ARM64，重复运行保留节点凭证。它只处理主机入站规则；在现有 VPS 上仍需自行放行云安全组 TCP 443 / 8080，并保证出站网络可用。自定义 nftables 规则不自动改写。控制端 `off` **不能卸载手工在其他 VPS 安装的服务**。

## 验证范围

配置读取、覆盖优先级、参数校验及磁盘选择测试：`python -m unittest discover -s tests -v`，全部使用本地模拟数据，不创建云资源。

当前开发环境完成了模拟云流程测试、官方 OpenAPI 参数核对，以及真实 sing-box / Mihomo 配置解析和本地 REALITY 转发测试；**尚未用真实阿里云凭证跑通完整创建到删除流程**。不同地域的库存、配额、镜像、计费和云端行为仍需要首轮实测。

官方参考：[RunInstances](https://help.aliyun.com/zh/ecs/developer-reference/api-ecs-2014-05-26-runinstances)、[库存查询](https://help.aliyun.com/zh/ecs/developer-reference/api-ecs-2014-05-26-describeavailableresource)、[安全组规则](https://help.aliyun.com/zh/ecs/user-guide/security-group-rules/)、[ECS 文档](https://help.aliyun.com/zh/ecs/)。

## RAM 权限策略

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecs:DescribeInstanceTypes",
        "ecs:DescribeAvailableResource",
        "ecs:DescribeImages",
        "ecs:DescribeInstances",
        "ecs:RunInstances",
        "ecs:StartInstance",
        "ecs:DeleteInstance",
        "ecs:CreateSecurityGroup",
        "ecs:DescribeSecurityGroups",
        "ecs:AuthorizeSecurityGroup",
        "ecs:AuthorizeSecurityGroupEgress",
        "ecs:DeleteSecurityGroup",
        "ecs:TagResources",
        "vpc:CreateVpc",
        "vpc:DescribeVpcs",
        "vpc:DeleteVpc",
        "vpc:CreateVSwitch",
        "vpc:DescribeVSwitches",
        "vpc:DeleteVSwitch",
        "vpc:TagResources"
      ],
      "Resource": "*"
    }
  ]
}
```

## 许可证与第三方说明

许可证：GPL-3.0，见 [LICENSE](LICENSE)。

The original deployment approach was informed by the open-source sing-box-yg
project (https://github.com/yonggekkk/sing-box-yg). This repository retains only
a small single-protocol deployment path and implements its own cloud lifecycle
controller. It does not download or execute that project's scripts at runtime.

The server bootstrap downloads official sing-box release binaries from
https://github.com/SagerNet/sing-box and verifies pinned archive SHA256 hashes.
sing-box is a separate GPL-licensed project; its sources and licensing terms are
available at the upstream repository. No sing-box binary is bundled here.

Mihomo (https://github.com/MetaCubeX/mihomo) is an optional client compatible
with the generated subscription. No Mihomo binary is bundled here.
