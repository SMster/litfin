"""Build a publishable bundle of the dashboard, for a static host.

WHY THIS SHAPE. The dashboard is one self-contained HTML file with its data
and behaviour inlined -- sorting, filtering, the coverage map, the expandable
rows, all of it. It needs no server. So the smallest correct way to "deploy
the app" is to put that file behind access control and keep collection on the
machine where it already runs.

That is not a compromise; it is strictly better than hosting the pipeline:

  * the hosted box does ZERO fetching, so the CourtListener scope question
    does not arise for it at all
  * no API key, no database, and no credential ever leaves your machine
  * nothing on the host can spend money
  * a static file has no attack surface worth the name

THE ONE THING THAT CAN GO WRONG, and the reason for the guard below: this
file names real parties in real litigation, carries damages estimates, and
describes how their cases might be monetized. Published to a plain static host
with no access control it is world-readable and search-indexable. GitHub
Pages, a naked S3 bucket, and a default Netlify/Vercel/Cloudflare Pages site
are ALL public by default.

So `build()` writes the bundle and `assert_target_protected()` refuses to call
anything public. The check is crude on purpose -- it cannot verify a host's
auth config from here -- but it forces the operator to state, in writing, what
is protecting it.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Hosts whose DEFAULT posture is world-readable. Naming them is more useful
# than a generic warning: these are exactly the ones somebody reaches for.
PUBLIC_BY_DEFAULT = {
    "github.io": "GitHub Pages serves every file publicly. There is no "
                 "access control on a public repo, and a private repo's Pages "
                 "site is still public on the free plan.",
    "githubusercontent.com": "Raw GitHub content is world-readable.",
    "s3.amazonaws.com": "A bucket website endpoint has no authentication.",
    "storage.googleapis.com": "Public GCS objects have no authentication.",
    "surge.sh": "Surge sites are public.",
    "neocities.org": "Neocities sites are public.",
}


class UnprotectedTarget(RuntimeError):
    """Raised rather than publishing case analysis to an open host."""


@dataclass(slots=True)
class Bundle:
    directory: Path
    files: list[Path] = field(default_factory=list)
    matters: int = 0
    generated_at: str = ""

    @property
    def total_bytes(self) -> int:
        return sum(f.stat().st_size for f in self.files if f.is_file())

    def to_text(self) -> str:
        lines = [
            f"Bundle: {self.directory}",
            f"  matters : {self.matters}",
            f"  files   : {len(self.files)} ({self.total_bytes / 1024:.0f} KB)",
        ]
        for f in self.files:
            lines.append(f"     {f.name}")
        return "\n".join(lines)


def assert_target_protected(target: str, *, protected_by: str = "") -> None:
    """Refuse to publish to somewhere obviously world-readable.

    `protected_by` is a free-text assertion from the operator naming what
    guards the target -- "Cloudflare Access, allowlist cesar+sean". It is not
    verifiable from here, and pretending otherwise would be worse than asking:
    the value of the check is that publishing requires SAYING what protects
    it, which is the moment somebody notices nothing does.
    """
    lowered = (target or "").lower()
    for host, why in PUBLIC_BY_DEFAULT.items():
        if host in lowered:
            raise UnprotectedTarget(
                f"Refusing to publish to {target!r}.\n\n{why}\n\n"
                f"This dashboard names real parties in real litigation, "
                f"carries damages estimates, and describes how their cases "
                f"might be monetized. It is confidential work product, not a "
                f"public site.\n\n"
                f"Use a host with access control -- Cloudflare Pages behind "
                f"Cloudflare Access is free and does one-time-PIN auth against "
                f"an email allowlist."
            )
    if not protected_by.strip():
        raise UnprotectedTarget(
            "Refusing to publish without --protected-by.\n\n"
            "Name what restricts access to this target, e.g.:\n"
            "  --protected-by \"Cloudflare Access, allowlist of 2 emails\"\n\n"
            "It is recorded in the bundle manifest. If you cannot name "
            "anything, that is the answer: the dashboard is confidential work "
            "product and must not go to an open host."
        )


def build(
    db, cfg, out_dir: Path, *, protected_by: str = "", limit: int | None = None,
) -> Bundle:
    """Render the dashboard and export into a directory ready to upload."""
    from ..deliver import dashboard, dataset, excel

    out_dir = Path(out_dir)
    if out_dir.exists():
        # A stale row from a previous publish is worse than no row: it looks
        # current and is not.
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    data = dataset.load(db, cfg, limit=limit)

    index = out_dir / "index.html"
    index.write_text(dashboard.render(data), encoding="utf-8")

    stamp = data.generated_at[:10]
    xlsx = out_dir / f"litfin-prospects-{stamp}.xlsx"
    excel.build(data, xlsx, limit=limit)

    # Belt and braces against a host that ignores its own defaults.
    (out_dir / "robots.txt").write_text(
        "# Confidential work product. Not for indexing.\n"
        "User-agent: *\nDisallow: /\n",
        encoding="utf-8",
    )
    (out_dir / "_headers").write_text(
        # Cloudflare Pages / Netlify honor this file.
        "/*\n"
        "  X-Robots-Tag: noindex, nofollow, noarchive\n"
        "  X-Frame-Options: DENY\n"
        "  X-Content-Type-Options: nosniff\n"
        "  Referrer-Policy: no-referrer\n"
        "  Cache-Control: no-store\n",
        encoding="utf-8",
    )

    manifest = {
        "generated_at": data.generated_at,
        "matters": len(data.prospects),
        "purpose": data.purpose,
        "protected_by": protected_by,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "contains": (
            "Case analysis naming real parties in real litigation, with "
            "damages estimates and monetization commentary. Confidential "
            "work product. Do not publish to an unauthenticated host."
        ),
        # No pipeline runs here. Recorded so a future reader knows the hosted
        # artifact never touched a third-party source.
        "fetches_anything": False,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    files = sorted(p for p in out_dir.iterdir() if p.is_file())
    return Bundle(
        directory=out_dir, files=files,
        matters=len(data.prospects), generated_at=data.generated_at,
    )
