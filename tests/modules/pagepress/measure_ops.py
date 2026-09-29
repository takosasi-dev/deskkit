# 実測用の子プロセス(test_measure_real.py から呼ぶ)。1つの操作を本番と同じ部品(build・compress・sanitize・jobs)で行い、
# 所要時間とこのプロセスの作業セットの最大値(psutil の peak_wset)を JSON で標準出力に出す。
# 使い方: python -m tests.modules.pagepress.measure_ops <op> <出力先フォルダ> <入力...>   op: probe / merge / split / compress_<段階>
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    import psutil

    from deskkit.modules.pagepress import jobs as J
    from deskkit.modules.pagepress import naming, reader
    from deskkit.modules.pagepress.oplog import OpsLog

    op, out_dir, inputs = argv[0], Path(argv[1]), [Path(a) for a in argv[2:]]
    out_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("measure")
    t0 = time.perf_counter()
    entries = [reader.probe(i, p) for i, p in enumerate(inputs)]
    t_probe = time.perf_counter() - t0
    res: dict[str, object] = {"op": op, "probe_s": round(t_probe, 2), "pages": sum(e.pages for e in entries),
                              "in_bytes": sum(e.size for e in entries)}
    if op != "probe":
        env = J.Env(OpsLog(out_dir / "ops.jsonl"), naming.Pending(out_dir / "pending.json"), log,
                    fallback_dir=lambda: out_dir / "fallback")
        if op == "merge":
            spec = J.JobSpec("merge", entries)
        elif op == "split":
            spec = J.JobSpec("split", entries, split_mode="single")
        else:
            spec = J.JobSpec("compress", entries, level=op.split("_", 1)[1])
        st = J.JobStatus(spec.op)
        st.cancel = threading.Event()
        t1 = time.perf_counter()
        J.run(spec, st, env)
        res["run_s"] = round(time.perf_counter() - t1, 2)
        res["state"] = st.state
        res["message"] = st.message
        res["outputs"] = [(r.text, r.size) for r in st.rows]
    mi = psutil.Process().memory_info()
    res["peak_wset_mb"] = round(getattr(mi, "peak_wset", mi.rss) / 1_000_000)
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
