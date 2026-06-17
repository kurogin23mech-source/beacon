<!-- ms-80 e-1823: 取り込み戦略の構造防御 -->

## 要点 (= この PR で何ができるようになるか)

<!-- 非開発者にも読める 1-2 行で。実装手段ではなくユーザー価値で。 -->

## なぜ (= 背景・動機)

<!-- どの MS / Operation / Issue から来たか。SPEC doc id があれば添える。 -->

## どこを見てほしいか

<!-- reviewer が「ここから読めば全体が分かる」と感じる起点 1-2 箇所。 -->

## merge 条件 (= AC)

- [ ] テスト通過 (= CI green)
- [ ] non-developer が要点欄を読んで意味が取れる
- [ ] 関連 task / SPEC が更新済 (= beacon task done / SPEC 追記)

---

## ⚠ 取り込み方法 (= merge strategy)

**`gh pr merge --merge`** で merge commit を作る (= fast-forward merge)。

- ✗ `--rebase` 禁止 (= hash 再生成で beacon entry が dead hash になる)
- ✗ `--squash` 禁止 (= 同上、commit 単位 1:1 の trace が壊れる)
- ✓ `--merge` (= linear history を維持しつつ hash 保持)

理由: Beacon の commit 記録 (= `beacon log` / `beacon pr` entry) は 7 文字 hash で紐付けているため、rebase / squash で hash が変わると過去エントリが「存在しない commit を指す dead hash」 になる。

詳細: CORE doc `0KqFUbmJ7V0lmJZcW230` (= PR の取り込み戦略: hash 保持と beacon entry 整合) 参照。

<!--
GitHub UI でも merge ボタン押す時は同じ。Settings > General > Pull Requests で:
  - Allow merge commits: ✓ ON (= 既定動線)
  - Allow squash merging: ✗ OFF (= 構造禁止)
  - Allow rebase merging: ✗ OFF (= 構造禁止)
を設定すれば UI 経路も塞げる (= 別 task、repo admin 操作)。
-->
