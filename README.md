# esxi-tui-manager

ESXi / vCenter 上の仮想マシンを TUI で一覧表示し、状態監視、Power On、Guest Shutdown を行う Python ツールです。

## 重要: 依存パッケージ

このツールに必要なのは `pyVmomi` です。

`pyvim` という別パッケージを入れても、`from pyVim.connect import SmartConnect` は解決できません。
必ず以下のように `python3 -m pip` で、実行に使う Python に対してインストールしてください。

```bash
python3 -m pip install -r requirements.txt
# または
python3 -m pip install 'pyVmomi>=8.0.0'
```

確認:

```bash
python3 - <<'PY'
from pyVim.connect import SmartConnect
from pyVmomi import vim
print('pyVmomi import OK')
PY
```

## セットアップ推奨

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

## 実行例

### ESXi へ直接接続

```bash
python3 esxi_tui.py --host <vCenter or ESXi IPaddress> --user root --insecure
```

### vCenter へ接続

```bash
python3 esxi_tui.py \
  --host vcsa.example.local \
  --user administrator@vsphere.local \
  --insecure \
  --interval 5
```

### パスワードを環境変数で渡す

```bash
export VSPHERE_PASSWORD='your-password'
python3 esxi_tui.py --host vcsa.example.local --user administrator@vsphere.local --insecure
```

## キー操作

| キー | 動作 |
|---|---|
| ↑ / ↓ または j / k | VM 選択 |
| p | 選択 VM を Power On |
| s | 選択 VM に Guest Shutdown を送信 |
| r | 手動更新 |
| / | VM 名フィルタ |
| c | フィルタ解除 |
| q または Esc | 終了 |

## 機能

- VM 一覧表示
- 電源状態の監視
- VMware Tools 状態の表示
- Guest Heartbeat の表示
- IP アドレス表示
- 選択 VM の Power On
- 選択 VM の Guest Shutdown
- VM 名フィルタ

Guest Shutdown は VMware Tools がゲスト OS 内で動作している必要があります。誤操作防止のため、強制 Power Off は実装していません。

## 必要権限の目安

- VM 一覧取得: System.View 相当
- Power On: `VirtualMachine.Interact.PowerOn`
- Guest Shutdown: `VirtualMachine.Interact.PowerOff`

## 注意

- `--insecure` は検証環境や自己署名証明書向けです。本番では証明書検証を有効にしてください。
- Guest Shutdown は API 呼び出し後すぐ戻ります。実際に OS が停止するまで、TUI の状態更新を見て確認してください。
- vCenter 経由で多数 VM を扱う場合、必要に応じて `--filter` を使って対象を絞ってください。
