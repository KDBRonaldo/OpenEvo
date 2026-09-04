# EvoLab 自部署模型服务器要求

本文说明 EvoLab 自部署模型模式的服务器要求，并严格区分：

- **必须预先具备的条件**：启动器无法替用户创建或补齐；
- **可以自动准备的软件**：用户不需要提前安装。

本文面向希望从一台新购云服务器开始，通过 `evolab webui` 完成自动部署的用户。当前推荐的服务器形态是具有 NVIDIA GPU 的完整 Ubuntu 虚拟机。

## 必须预先具备的条件

### 1. NVIDIA GPU

服务器必须实际分配有 NVIDIA GPU。当前自部署运行时不支持 CPU-only 服务器、AMD GPU 或其他加速器。

当前发布内置的最小自部署模型配置为 `Qwen/Qwen3-0.6B`，硬件下限如下：

| 资源 | 最低要求 | 推荐配置 |
| --- | ---: | ---: |
| GPU 显存 | 8 GiB | 16 GiB 或更多 |
| 系统内存 | 16 GiB | 32 GiB 或更多 |
| 可用磁盘 | 30 GiB | 60–100 GiB |
| GPU 数量 | 1 | 1 |

启动 vLLM 时，当前运行时还要求所选 GPU 至少约 85% 的显存处于空闲状态。因此，即使显存总量达标，已有任务占用大量显存时仍会拒绝启动。

### 2. 可自动管理的 Linux 主机

若希望启动器从近似空白的服务器自动准备全部软件，服务器必须是：

- 完整 Linux 虚拟机，而不是受限的开发容器；
- Ubuntu，推荐 Ubuntu Server 22.04 或 24.04；
- PID 1 为 `systemd`；
- `x86_64`/`amd64` 架构；
- SSH 用户为 `root`，或者拥有免密码 `sudo`。

免密码 `sudo` 可通过以下命令检查：

```bash
sudo -n true
```

命令返回成功且不询问密码，才表示启动器能够无人值守地安装系统组件。ARM 服务器和非 Ubuntu GPU 主机目前不属于自动准备的正式验证范围。

### 3. 可用的 SSH 连接

客户端必须能够通过普通 OpenSSH 连接服务器：

- 服务器具有客户端能够访问的公网地址，或者已有可用的跳板机路径；
- SSH 端口可达，通常为 TCP 22；
- 使用密钥认证；
- SSH 用户的 `$HOME` 目录可写；
- 同一个 SSH 用户能够运行安装后的 EvoLab 服务。

EvoLab 的 Web Layer 和 daemon 只绑定服务器回环地址。除 SSH 端口外，不需要向公网开放 vLLM、WebUI 或 daemon 端口。

### 4. 出站网络连接

服务器必须能够进行 DNS 查询和出站 HTTPS 访问。根据服务器上已经存在的软件，首次准备可能需要访问：

- Ubuntu 软件源；
- Astral 的 uv 安装与 Python 下载服务；
- 配置的 Python 包源；
- Docker Hub；
- NVIDIA Container Toolkit 的签名软件源；
- Hugging Face，或管理员配置的 `HF_ENDPOINT` 镜像。

OpenEvo 产品源码不由服务器从 GitHub 拉取；启动器会从客户端通过 SSH 上传经过校验的 Release Bundle。但是 Python 依赖、容器镜像和模型文件仍可能需要服务器访问外网。

## 不需要预先安装的软件

在满足上述条件的标准 Ubuntu 虚拟机上，EvoLab 启动器可以自动检查并准备：

- Python 基础命令；
- `curl`、CA 证书和 GNU coreutils；
- uv；
- EvoLab 使用的 Python 3.11 环境和锁定依赖；
- Codex CLI 相关环境（使用订阅执行模式时）；
- Ubuntu 推荐的 NVIDIA 计算驱动；
- Docker Engine；
- NVIDIA Container Toolkit；
- Docker 的 NVIDIA Runtime 配置；
- 固定版本的 vLLM Docker 镜像；
- 选定并校验的 Hugging Face 模型文件。

因此，Docker、NVIDIA Container Toolkit、uv 和 Python 3.11 都不应作为普通用户的手工安装步骤。

## NVIDIA 驱动、CUDA 和 cuDNN

NVIDIA 驱动不是标准 Ubuntu 虚拟机上的绝对前置条件。服务器存在 NVIDIA GPU、使用 `systemd`，并且 SSH 用户具有 root 或免密码 `sudo` 时，启动器可以安装 Ubuntu 推荐的计算驱动。新驱动如果必须在重启后才能加载，用户只需重启服务器一次，再重新运行同一条 `evolab webui` 命令。

宿主机不要求预先安装 CUDA Toolkit 或 cuDNN。vLLM 在固定的 Docker 镜像中运行，镜像携带匹配的 CUDA 用户态环境；宿主机必须提供的是可工作的 NVIDIA 内核驱动和容器 GPU 访问能力。

选择云厂商提供的“自动安装 GPU 驱动”镜像仍然是推荐做法，因为它通常比首次启动时再安装驱动更快，也能减少一次重启。

## 容器化 GPU 租赁的限制

HAI、在线 Notebook 和部分按小时 GPU 平台提供的是容器，而不是完整虚拟机。此类服务器通常没有 `systemd`，启动器不会在里面安装驱动、启动 Docker daemon、修改 Docker 配置或修改用户权限。

容器化服务器只有在以下能力已经由云厂商提供时才能运行当前自部署模式：

- `nvidia-smi` 在容器内可用；
- 当前 SSH 用户可以直接执行 `docker info`；
- Docker daemon 已经运行；
- `docker info --format '{{json .Runtimes}}'` 包含 `nvidia` Runtime；
- 平台允许从当前容器再启动 GPU Docker 容器。

否则应改用完整 Ubuntu GPU 虚拟机。对面向非技术用户的产品部署，不应把受限容器作为默认服务器类型。

## 采购检查清单

购买服务器时可以直接按以下清单筛选：

- [ ] 完整 Ubuntu 22.04/24.04 GPU 虚拟机
- [ ] `x86_64`/`amd64`
- [ ] 单张 NVIDIA GPU
- [ ] 至少 8 GiB 显存，推荐 16 GiB
- [ ] 至少 16 GiB 内存，推荐 32 GiB
- [ ] 至少 30 GiB 可用磁盘，推荐 60–100 GiB
- [ ] root 或免密码 `sudo`
- [ ] 公网 SSH 或已配置的 SSH 跳板路径
- [ ] 正常的 DNS 和出站 HTTPS
- [ ] 安全组开放 SSH 端口
- [ ] 不向公网开放 vLLM/WebUI/daemon 端口

## 创建后的基础验证

首次登录服务器后，可以运行：

```bash
uname -m
ps -p 1 -o comm=
sudo -n true && echo "passwordless sudo: ready"
nvidia-smi
free -h
df -h "$HOME"
```

理想结果为：

- `uname -m` 输出 `x86_64`；
- PID 1 输出 `systemd`；
- `sudo -n true` 不要求密码；
- `nvidia-smi` 能看到 NVIDIA GPU；
- 内存、磁盘和显存满足上述下限。

如果云厂商仍在后台安装 GPU 驱动，应等待其完成后再执行验证，不要同时安装或修改 GPU 软件。

## 已验证的采购示例

以下配置符合当前小模型功能测试要求：

```text
操作系统：Ubuntu Server 24.04 LTS 64 位
实例：腾讯云 GN7.2XLARGE32
GPU：1 × NVIDIA T4，16 GiB 显存
CPU：8 核
系统内存：32 GiB
系统盘：100 GiB SSD
登录：ubuntu 用户 + SSH 密钥
权限：免密码 sudo
网络：公网 SSH + 出站 HTTPS
```

该配置用于功能验证时已经足够，不需要 V100、A100、多卡或 32 GiB 以上显存。
