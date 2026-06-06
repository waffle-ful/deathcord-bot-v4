// =============================================================================
// refactor-implement-review
// -----------------------------------------------------------------------------
// 「設計(あなた＋Opus・会話の中) → 実装(Sonnetサブエージェント) → 敵対的レビュー(Opus)」
// パターンの再利用可能エンジン。
//
// 設計（消す/残す/どう変えるの線引き）は会話の中で人間とOpusが先に作る。
// このワークフローは、その確定スペックを受け取って「実装→客観ゲート検証」を自動化する部品。
//
// 呼び出し方:
//   Workflow({
//     name: "refactor-implement-review",        // または scriptPath で直接指定
//     args: {
//       repo: "/path/to/repo",  // 作業ディレクトリ(必須)
//       file: "main.py",                                  // 編集対象ファイル(必須・repo相対 or 絶対)
//       spec: "...自然文の実装スペック(何をどう削除/変更するか・アンカー・残すもの)...", // (必須)
//       deletedSymbols: ["SCARY_CHANCE", "trigger_mimic"],  // 変更後 repo全体で 0ヒットであるべき識別子
//       keptSymbols:    ["MIMIC_DEEP_PROMPT", "mimic_cmd"], // file内に残っているべき識別子
//       compileCmd: "python -m py_compile main.py",         // 構文チェックコマンド(省略時はこの既定値)
//       implementerModel: "sonnet"                          // 実装worker(省略時 sonnet)
//     }
//   })
//
// 戻り値: { impl, gate, diffAudit } — 実装ログ＋2レビュアーの構造化判定
// =============================================================================

export const meta = {
  name: 'refactor-implement-review',
  description: 'Apply a pre-designed refactor/cleanup spec with a cheap worker, then adversarially verify (compile + symbol grep + pure-deletion/diff audit) with Opus reviewers',
  whenToUse: 'You already have a precise spec (what to change, which symbols must vanish, which must stay). Use this to execute it safely: Sonnet implements, two Opus reviewers gate it on objective checks. Pass the spec via args.',
  phases: [
    { title: '実装', detail: 'Worker applies the spec by string anchors, then self-compiles' },
    { title: 'レビュー', detail: 'Opus reviewers run compile+symbol gate (A) and diff audit (B) in parallel' },
  ],
}

// ---- args の取り出しと最小バリデーション ------------------------------------
const a = args || {}
const REPO = a.repo
const FILE = a.file
const SPEC = a.spec
const DELETED = Array.isArray(a.deletedSymbols) ? a.deletedSymbols : []
const KEPT = Array.isArray(a.keptSymbols) ? a.keptSymbols : []
const IMPL_MODEL = a.implementerModel || 'sonnet'
const COMPILE_CMD = a.compileCmd || 'python -m py_compile ' + (FILE || '')

if (!REPO || !FILE || !SPEC) {
  log('❌ args が不足しています。repo / file / spec は必須です。例:')
  log('   Workflow({name:"refactor-implement-review", args:{repo:"...", file:"main.py", spec:"...", deletedSymbols:[...], keptSymbols:[...]}})')
  throw new Error('refactor-implement-review: args.repo, args.file, args.spec are required')
}

const delAlt = DELETED.join('\\|')   // grep BRE alternation
const keptAlt = KEPT.join('\\|')

// =============================================================================
phase('実装')
// =============================================================================
const IMPL_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    editsApplied: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          target: { type: 'string' },
          status: { type: 'string', enum: ['deleted', 'changed', 'skipped', 'failed'] },
          note: { type: 'string' },
        },
        required: ['target', 'status', 'note'],
      },
    },
    selfCompileOk: { type: 'boolean' },
    summary: { type: 'string' },
  },
  required: ['editsApplied', 'selfCompileOk', 'summary'],
}

const impl = await agent(
  `あなたは熟練のリファクタ実装者です。以下の確定スペックに従い、対象ファイルを正確に編集してください。

作業ディレクトリ: ${REPO}
対象ファイル: ${FILE}

【手順】
1. まず対象領域を Read し、現在の正確な文字列（インデント・空白・全角記号を含む）を取得する。
2. Edit ツールで変更を適用する。old_string はファイルと厳密一致させ、一意になる十分な長さを取る。
   行番号では指定しない（編集のたびに下の行番号がズレるため、必ず文字列アンカーで）。
3. スペックに無い変更（リフォーマット・import整理・他ファイル編集）は一切しない。
4. 全変更を適用したら、最後に \`cd "${REPO}" && ${COMPILE_CMD}\` を実行し構文エラーが無いことを自分でも確認する
   （python が無ければ py / python3 を試す）。

あなたの最終出力(StructuredOutput)はそのまま機械処理されます。人間向けの挨拶は不要。
説明だけで終わらせず、実際に Edit を適用すること。

================ 実装スペック ================
${SPEC}
=============================================`,
  { label: `implement:${FILE}`, phase: '実装', model: IMPL_MODEL, schema: IMPL_SCHEMA }
)

// =============================================================================
phase('レビュー')
// =============================================================================
const GATE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    compileOk: { type: 'boolean' },
    compileOutput: { type: 'string' },
    deletedSymbolsZeroHits: { type: 'boolean' },
    deletedSymbolHits: { type: 'array', items: { type: 'string' } },
    keptSymbolsPresent: { type: 'boolean' },
    missingKeptSymbols: { type: 'array', items: { type: 'string' } },
    verdict: { type: 'string', enum: ['pass', 'fail'] },
    details: { type: 'string' },
  },
  required: ['compileOk', 'compileOutput', 'deletedSymbolsZeroHits', 'deletedSymbolHits', 'keptSymbolsPresent', 'missingKeptSymbols', 'verdict', 'details'],
}

const DIFF_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    matchesSpec: { type: 'boolean', description: 'diff が spec の範囲と完全一致するか' },
    unexpectedChanges: { type: 'array', items: { type: 'string' } },
    loadBearingTouched: { type: 'boolean' },
    addedLines: { type: 'number', description: '純粋削除なら 0 のはず（追加行数）' },
    verdict: { type: 'string', enum: ['pass', 'fail'] },
    findings: { type: 'string' },
  },
  required: ['matchesSpec', 'unexpectedChanges', 'loadBearingTouched', 'addedLines', 'verdict', 'findings'],
}

const gateInstr = DELETED.length
  ? `2. 削除シンボルが消えたか: \`cd "${REPO}" && grep -rn --include=*.py "${delAlt}" .\` を実行。**0ヒット**であるべき。
   1件でもヒットしたら deletedSymbolHits に file:line を記録し deletedSymbolsZeroHits=false。`
  : `2. （deletedSymbols 指定なし。deletedSymbolsZeroHits=true / deletedSymbolHits=[] とせよ）`

const keptInstr = KEPT.length
  ? `3. 残すべきシンボルが生きているか: \`cd "${REPO}" && grep -n "${keptAlt}" ${FILE}\` を実行。
   指定シンボルが全て存在するべき。消えていたら missingKeptSymbols に記録し keptSymbolsPresent=false。`
  : `3. （keptSymbols 指定なし。keptSymbolsPresent=true / missingKeptSymbols=[] とせよ）`

const [gate, diffAudit] = await parallel([
  () => agent(
    `あなたは敵対的レビュアーです。${REPO}/${FILE} への変更が機械的ゲートを満たすか厳密に検証してください。疑わしきは fail。
Bash で実際にコマンドを走らせて確認すること（報告を鵜呑みにしない）。

1. 構文: \`cd "${REPO}" && ${COMPILE_CMD} && echo COMPILE_OK\`（python が無ければ py / python3）。エラー全文を compileOutput に。
${gateInstr}
${keptInstr}
4. 総合: 上記すべて満たせば verdict=pass、1つでも欠ければ fail。

あなたの最終出力はそのまま機械処理される。`,
    { label: 'review:gate', phase: 'レビュー', schema: GATE_SCHEMA }
  ),
  () => agent(
    `あなたは敵対的レビュアーです。${REPO}/${FILE} への変更が「スペックの範囲ちょうど」になっているか git diff を読んで監査してください。
過剰変更・過少変更・巻き添えを探す。疑わしきは fail。

手順:
1. \`cd "${REPO}" && git diff --numstat -- ${FILE}\` で追加/削除行数を確認（純粋削除を意図しているなら追加行は 0 のはず → addedLines に記録）。
2. \`cd "${REPO}" && git diff -- ${FILE}\` の全ハンクを読み、下記スペックの意図と完全一致するか確認する。
3. スペックが「残す」と明記したシンボルが diff で削除されていないか厳しく見る（特に共有ヘルパーの巻き添え）。
4. スペック外の変更（リフォーマット・無関係シンボル・他ファイル）があれば unexpectedChanges に列挙し fail。
   ※ diff は -- ${FILE} で限定済み。他ファイルの差分は対象外なので無視してよい。

================ スペック（判定基準）================
${SPEC}
==================================================

あなたの最終出力はそのまま機械処理される。`,
    { label: 'review:diff', phase: 'レビュー', schema: DIFF_SCHEMA }
  ),
])

const bothPass = gate && diffAudit && gate.verdict === 'pass' && diffAudit.verdict === 'pass'
log(bothPass ? '✅ 両レビュアー pass' : '⚠️ レビューで指摘あり — 戻り値を確認')

return { impl, gate, diffAudit, bothPass }
