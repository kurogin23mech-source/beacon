"""ms-96 e-2382 — vps-backup.sh / vps-restore.sh の挙動を PATH shim で検証する。

本番の mysqldump / aws / mysql には触れず、それらを shim (偽コマンド) に差し替えて、
スクリプトが「正しい S3 キーへ、一貫性オプション付きで退避するか」「パイプ途中の失敗を
成功と誤認しないか」を assert する。実 MySQL / 実 S3 での退避・復旧の実機確認は VPS 側
(docs/BACKUP_VPS.md の受入手順) に委ねる。test_vps_pull_deploy.py と同じ shim 方式。
"""
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUP = REPO_ROOT / "scripts" / "vps-backup.sh"
RESTORE = REPO_ROOT / "scripts" / "vps-restore.sh"


def _write_shims(bindir: Path, state: Path, *, dump_rc=0, aws_cp_rc=0):
    """Fake mysqldump / gzip / gunzip / aws / mysql / logger.

    - mysqldump : record argv to <state>/dump_args, emit fake SQL, exit dump_rc
    - aws       : `s3 cp - <dest>` records dest to <state>/s3_dest and drains
                  stdin (exit aws_cp_rc); `s3 cp <src> -` emits fake gz to stdout;
                  `s3 ls <uri>` prints a canned listing
    - mysql     : record argv to <state>/mysql_args, drain stdin, exit 0
    - gzip/gunzip: pass stdin→stdout (identity — we don't assert on compression)
    - logger    : no-op
    """
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "mysqldump").write_text(
        "#!/usr/bin/env bash\n"
        f'ST="{state}"\n'
        'printf "%s\\n" "$@" > "$ST/dump_args"\n'
        'echo "-- fake dump"\n'
        f'exit {dump_rc}\n'
    )
    (bindir / "aws").write_text(
        "#!/usr/bin/env bash\n"
        f'ST="{state}"\n'
        '# forms: aws s3 cp - <dest> | aws s3 cp <src> - | aws s3 ls <uri>\n'
        'if [ "$1" = "s3" ] && [ "$2" = "cp" ]; then\n'
        '  if [ "$3" = "-" ]; then echo "$4" > "$ST/s3_dest"; cat > /dev/null;'
        f' exit {aws_cp_rc}; \n'
        '  else echo "-- fake dump"; fi\n'
        'elif [ "$1" = "s3" ] && [ "$2" = "ls" ]; then\n'
        '  echo "2026-07-17 00:00:00  100 beacon-prod-20260717T000000Z.sql.gz"\n'
        '  echo "2026-07-17 06:00:00  100 beacon-prod-20260717T060000Z.sql.gz"\n'
        'fi\n'
        'exit 0\n'
    )
    (bindir / "mysql").write_text(
        "#!/usr/bin/env bash\n"
        f'ST="{state}"\n'
        'printf "%s\\n" "$@" >> "$ST/mysql_args"\n'
        'cat > /dev/null 2>&1 || true\n'
        'exit 0\n'
    )
    for name in ("gzip", "gunzip"):
        (bindir / name).write_text("#!/usr/bin/env bash\ncat\n")
    (bindir / "logger").write_text("#!/usr/bin/env bash\nexit 0\n")
    for f in ("mysqldump", "aws", "mysql", "gzip", "gunzip", "logger"):
        (bindir / f).chmod(0o755)


def _base_env(bindir: Path):
    return {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": os.environ.get("HOME", "/tmp"),
        "MYSQL_USER": "backup",
        "MYSQL_PASSWORD": "pw",
        "MYSQL_DB": "beacon",
        "BEACON_BACKUP_S3_BUCKET": "beacon-prod-backups",
        "BEACON_ENV": "prod",
        "BEACON_BACKUP_TIMESTAMP": "20260717T120000Z",
    }


def _run(script, bindir, extra_env=None, args=()):
    env = _base_env(bindir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(["bash", str(script), *args],
                          capture_output=True, text=True, env=env)


@pytest.fixture
def bins(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    bindir = tmp_path / "bin"
    _write_shims(bindir, state)
    return state, bindir


# --- backup ----------------------------------------------------------------

def test_backup_uploads_to_timestamped_env_scoped_key(bins):
    state, bindir = bins
    r = _run(BACKUP, bindir)
    assert r.returncode == 0, r.stderr
    dest = (state / "s3_dest").read_text().strip()
    assert dest == ("s3://beacon-prod-backups/mysql/prod/"
                    "beacon-prod-20260717T120000Z.sql.gz"), dest


def test_backup_uses_consistent_snapshot_flags(bins):
    state, bindir = bins
    r = _run(BACKUP, bindir)
    assert r.returncode == 0, r.stderr
    args = (state / "dump_args").read_text()
    # 稼働中でも一貫スナップショットが取れる InnoDB 向けオプション + 最小権限。
    assert "--single-transaction" in args
    assert "--no-tablespaces" in args
    assert "--user=backup" in args
    assert "beacon" in args  # target DB


def test_backup_fails_when_mysqldump_fails(bins, tmp_path):
    # mysqldump が失敗したら、gzip/aws が 0 で返っても退避は失敗扱い (PIPESTATUS)。
    state = tmp_path / "s2"
    state.mkdir()
    bindir = tmp_path / "bin2"
    _write_shims(bindir, state, dump_rc=2)
    r = _run(BACKUP, bindir)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "退避失敗" in (r.stdout + r.stderr)


def test_backup_requires_s3_bucket(bins):
    state, bindir = bins
    r = _run(BACKUP, bindir, extra_env={"BEACON_BACKUP_S3_BUCKET": ""})
    assert r.returncode == 2
    assert "バケット未設定" in (r.stdout + r.stderr)


def test_backup_requires_mysql_user(bins):
    state, bindir = bins
    r = _run(BACKUP, bindir, extra_env={"MYSQL_USER": ""})
    assert r.returncode == 2
    assert "user 未設定" in (r.stdout + r.stderr)


def test_backup_prefers_beacon_mysql_override(bins):
    # server/mysql_client.py と同じ優先順位: BEACON_MYSQL_* が MYSQL_* を上書きする。
    state, bindir = bins
    r = _run(BACKUP, bindir, extra_env={"BEACON_MYSQL_USER": "override_user"})
    assert r.returncode == 0, r.stderr
    assert "--user=override_user" in (state / "dump_args").read_text()


# --- restore / verify ------------------------------------------------------

def test_verify_restores_into_scratch_db_not_prod(bins):
    state, bindir = bins
    r = _run(RESTORE, bindir, args=("verify", "--latest"))
    assert r.returncode == 0, r.stderr
    mysql_args = (state / "mysql_args").read_text()
    # 検証は本番 DB (beacon) でなく beacon_restore_check に流す。
    assert "beacon_restore_check" in mysql_args
    # 本番 DB へ --one-database beacon で流していないこと。
    assert "--one-database\nbeacon\n" not in mysql_args


def test_verify_picks_latest_key(bins):
    state, bindir = bins
    r = _run(RESTORE, bindir, args=("verify", "--latest"))
    assert r.returncode == 0, r.stderr
    # aws s3 ls の末尾 (06:00) が最新として選ばれる。
    assert "20260717T060000Z" in (r.stdout + r.stderr)


def test_restore_into_prod_requires_force(bins):
    state, bindir = bins
    r = _run(RESTORE, bindir, args=("restore", "--latest", "--into", "beacon"))
    assert r.returncode == 2
    assert "--force" in (r.stdout + r.stderr)


def test_restore_into_prod_with_force_ok(bins):
    state, bindir = bins
    r = _run(RESTORE, bindir,
             args=("restore", "--latest", "--into", "beacon", "--force"))
    assert r.returncode == 0, r.stderr
    assert "復旧成功" in (r.stdout + r.stderr)
