# 我的 Steam 图床

把 Steam 截图云当图床用的静态画廊。索引很轻，图片全在 Steam 的 CDN 上。

| 文件 | 作用 |
|---|---|
| `index.html` | 画廊页，按类切换，类目清单已内联，不再单独请求 |
| `p.json` / `p.full.json` | 普通图索引：路径版 / 完整 URL 版 |
| `r.json` / `r.full.json` | R 级图索引：路径版 / 完整 URL 版 |
| `.nojekyll` | 阻止 GitHub Pages 走 Jekyll，避免下划线开头的文件被忽略 |

每一类都是 `{"pics": [...]}`。路径版只存 CDN 路径片段，
图片真实地址 = `https://images.steamusercontent.com/ugc/` + 路径。完整 URL 版可以直接当图源填。

当前索引：**0** 条（两类合计）

壁纸软件的图源按需填：

- 只要普通图：`.../p.json`
- 只要 R 级图：`.../r.json`
- 不想分：两个都加上

同一张图只会出现在其中一类里，不会重复。

## 部署

```bash
git init -b main && git add -A && git commit -m "init gallery"
git remote add origin https://github.com/<用户名>/<仓库名>.git
git push -u origin main
```

仓库 `Settings` → `Pages` → Source 选 **Deploy from a branch**，分支 `main`，目录 `/(root)`。
等 1~2 分钟，访问 `https://<用户名>.github.io/<仓库名>/`。

## 更新图片

```bash
python steambed.py prep ./pics --appid <appid>     # Steam 已退出
# 打开 Steam 手动上传新截图
python steambed.py sync --steamid <你的ID> --appid <appid> --site ./site
```

也可以让 `grab` 一次做完：`grab ... --site ./site` 会在抓完链接后自动校验、
重建索引并推送，不用再跑 `sync`。

`sync` 做四件事：绕过 CDN 缓存校验每条链接 -> 剔除已失效的 -> 重建两类索引 ->
提交并推送到 GitHub。**云端删了图，跑一次 sync，索引里那条就自动消失**。
双击根目录的 `sync.cmd` 等价于跑这条命令。

`prep` 会按清单跳过已导入的图片，追加新图后重跑不会产生重复项。

### 截图设为私密时

私密截图不会出现在公开列表里，`grab` / `sync` 必须带登录 Cookie：

```bash
python steambed.py sync --steamid <你的ID> --cookie "steamLoginSecure=..." --site ./site
```

## 注意事项

- **公开可见**：图片挂在 Steam 资料页截图库里，任何人都能看。不要放私密图片。
- **无法管理**：Steam 没有公开的删除/替换接口，图片只能在客户端手动删。
- **国内访问**：大陆直连 Steam 图片 CDN 长期不稳定，面向国内读者务必先实测。
- **无 SLA**：这是把 Steam 当免费存储用，不符合其设计用途，理论上图片可能被清理。
