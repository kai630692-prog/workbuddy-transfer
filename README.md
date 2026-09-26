# workbuddy-transfer

在 **Windows 同一台电脑**上，将 WorkBuddy 旧账号的本地会话及其关联文件保留并重新关联到新账号。它是一个独立的 Python 脚本，无第三方依赖；只处理本地数据，不迁移登录凭证、云端记忆或跨设备同步数据。

> **先备份，后换号。** 这是对 WorkBuddy 本地数据库格式的操作，不是官方账号迁移接口。请在使用前读完步骤，并在新账号中实际打开旧会话核对结果。

## 环境

- Windows 10/11、Python 3.9 或更新版本（WorkBuddy 自带的 Python 也可以）。
- 本机 WorkBuddy 数据目录默认为 `%USERPROFILE%\.workbuddy`。
- 足够的磁盘空间保存两份本地数据快照。快照包括会话正文、关联记录及部分本地配置，可能含私人信息或令牌，**不要上传到 GitHub 或分享给他人**。

## 使用

在 PowerShell 中进入本项目目录。下文的 `python` 可按需替换成 `py -3`；`workbuddy-transfer.cmd` 也接受相同参数。

### 1. 旧账号登录状态下备份

保存工作并从系统托盘**完全退出** WorkBuddy，然后运行：

```powershell
python .\workbuddy_transfer.py prepare
```

输出会给出备份目录，例如 `backups\old-20260101-120000`。把这个路径记下来。如果本地数据库已有多个账号，命令会要求用 `prepare --old-uid <旧账号UUID>` 明确选择。

备份使用 SQLite 备份接口复制索引，并逐文件校验本地记录。备份未成功时不要换号。

### 2. 登录新账号

打开 WorkBuddy，退出旧账号并登录新账号。在新账号中**新建一条简短会话**，随后再次从托盘完全退出 WorkBuddy。

### 3. 查看候选新账号

```powershell
python .\workbuddy_transfer.py inspect --snapshot .\backups\old-20260101-120000
```

检查 `new_uid_candidates`。只应出现你刚创建会话的新账号 UID；若有多个候选，先核实后再继续。

### 4. 迁移

```powershell
python .\workbuddy_transfer.py migrate --snapshot .\backups\old-20260101-120000 --new-uid <新账号UUID>
```

脚本会先保存一份 `before-migration-*` 快照，再补回登录过程中可能消失的旧会话索引、缺失的原始会话文件及关联记录，并校验数据库和正文数量。已有文件不会被覆盖；如新账号已有自己的本地记忆，旧记忆另存为 `*_memory.from-old.md`，不会自动并入云端记忆。

### 5. 打开检查

重新打开 WorkBuddy，确认旧会话出现在新账号历史列表里。至少点开一条长会话，检查正文、图片及关联产物。`migration-report-*.json` 是本机校验结果，界面检查仍然必要。

## 数据范围与限制

- **处理**：`workbuddy.db` 中的会话归属及会话用量记录；`projects` 正文；附件索引、文件变更与历史、blobs 等本地关联文件。
- **保留但不自动合并**：旧账号本地记忆和账号配置，位于备份中；本地记忆会另存，不覆盖新账号现有记忆。
- **不处理**：登录凭证、云端记忆、云端检索、其他设备的数据。软件更新可能改变本地格式；表结构不兼容时脚本会停止。
- **回退**：迁移前快照保存了新账号尚未改写的数据库和文件。若需回退，先完全退出 WorkBuddy，并保留迁移后数据，再根据快照恢复；不要直接覆盖已产生新会话的数据库。

## 自定义路径与验证

`--root` 可指定其他 WorkBuddy 数据目录，`--backups` 可将备份放在项目目录以外；这两个参数应放在子命令前。例如：

```powershell
python .\workbuddy_transfer.py --backups D:\private-workbuddy-backups prepare
```

运行不含真实账号数据的测试：

```powershell
python -m unittest discover -s tests -v
```

本仓库只收录脚本、测试和说明。`.gitignore` 采用源文件白名单，备份、会话、记忆、数据库和迁移报告不会被普通 `git add` 纳入提交。

