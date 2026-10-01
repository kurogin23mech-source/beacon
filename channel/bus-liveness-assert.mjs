// ms-173 / e-6583 — 「生存を主張してよいか」の純粋な判断。
//
// 背景 (実測 2026-10-01): 親 bclaude が死んで孤児 (PPID=1) になった bus.mjs が 3 本、
// 25 日間 prod へ WS をつないだまま 30 秒ごとに ping を送り続けていた。server は ping を
// 生存の真値として Redis の生存キー (score=now+60 / key TTL 70s) を延命するため、死んだ
// セッションが ws_live=true のまま 20 日以上居座った (last_poll_at は 500 時間前)。
//
// 構造的な誤りは「ソケットが開いて ping が鳴っている」を生存の証拠にしていたこと。ping は
// *タイマー* が鳴らすので、仕事をしている poll loop の死とも、セッション本体の死とも無関係に
// 鳴り続ける。しかも poll loop は pollOnce / heartbeat の例外を捕まえて回り続けるので
// 「ループが回っている」ことも証拠にならない (REST が死んでいても回る)。
//
// ここに置く 2 つの判断だけが生存主張の門。bus.mjs 側は状態 (最後に心拍が通った時刻 /
// 起動時の親 pid) を持つだけで、判断は持たない — 判断を純粋にしておくと、ソケットも
// プロセスも無しに全分岐を固定できる (lib/bus_liveness.derive_state と同じ作法)。

/** 生存主張を止めるべきか。
 *
 * 不変条件: **REST で生存報告 (writePollHeartbeat) が通っていないなら、WS で生存を
 * 主張しない。** 「送ろうとした」ではなく「server が受け取った」を真値にする。
 *
 * 回復可能な判断にしてある (= 恒久停止でなく一時停止)。一時的なクラウド障害で受信能力を
 * 自ら手放さないため。しきい値は心拍間隔 (15s) よりはるかに長く取り、健全な bridge が
 * 誤って黙ることを防ぐ — 誤って黙ると DM が届かなくなり、嘘より重い害になる。
 *
 * @param {number} lastHeartbeatOkAt 最後に心拍が *通った* epoch ms
 * @param {number} now 現在の epoch ms
 * @param {number} limitMs 許容する停滞 (ms)
 */
export function livenessAssertionStale(lastHeartbeatOkAt, now, limitMs) {
  if (!Number.isFinite(lastHeartbeatOkAt) || !Number.isFinite(now)) return false
  if (!Number.isFinite(limitMs) || limitMs <= 0) return false
  return (now - lastHeartbeatOkAt) > limitMs
}

/** 孤児 bridge か (= 親が死んで奉仕先が無いか)。
 *
 * 判定を「起動時は親が居た → いま 1 になった」という *里親付けの証跡* に限る理由が 2 つ:
 *
 * 1. 単に ppid === 1 を見ると、launchd / init / コンテナの PID 1 配下で正当に起動された
 *    bridge を即座に殺してしまう (= 起動直後に自滅して受信が死ぬ)。
 * 2. pid 生存確認 (kill(pid, 0)) を一切使わない。Windows では対象プロセスを *終了させて
 *    しまう* 既知の罠があるため。さらに Windows の孤児は ppid が 1 にならないので、
 *    win32 は検知対象から外す = 検知できない側 (何もしない) に倒す。
 *
 * どちらも「生きている bridge を誤って殺さない」側に倒してある。
 */
export function isOrphanedBridge(platform, initialPpid, currentPpid) {
  if (platform === 'win32') return false
  if (initialPpid === 1) return false   // 最初から PID 1 配下 = 里親付けではない
  if (!Number.isFinite(initialPpid) || !Number.isFinite(currentPpid)) return false
  return currentPpid === 1
}
