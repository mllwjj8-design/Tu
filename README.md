# 我的 Steam 图床

把 Steam 截图云当图床用的静态画廊。索引很轻，图片全在 Steam 的 CDN 上。

| 文件 | 作用 |
|---|---|
| `index.html` | 画廊页，运行时 `fetch("r.json")` 加载列表 |
| `r.json` | 索引，只存 CDN 路径片段，不存完整地址 |
| `.nojekyll` | 阻止 GitHub Pages 走 Jekyll，避免下划线开头的文件被忽略 |

图片真实地址 = `https://images.steamusercontent.com/ugc/` + `r.json` 里的路径

当前索引：**3** 条

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
python steambed.py grab --steamid <你的ID> --appid <appid> --resolve-names
python steambed.py export --site ./site
git add -A && git commit -m "update" && git push
```

`prep` 会按清单跳过已导入的图片，追加新图后重跑不会产生重复项。

## 注意事项

- **公开可见**：图片挂在 Steam 资料页截图库里，任何人都能看。不要放私密图片。
- **无法管理**：Steam 没有公开的删除/替换接口，图片只能在客户端手动删。
- **国内访问**：大陆直连 Steam 图片 CDN 长期不稳定，面向国内读者务必先实测。
- **无 SLA**：这是把 Steam 当免费存储用，不符合其设计用途，理论上图片可能被清理。
