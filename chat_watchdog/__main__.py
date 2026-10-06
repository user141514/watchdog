from __future__ import annotations

import sys


if len(sys.argv) > 1 and sys.argv[1] == "fixed-action":
    from .fixed_action_timer import main as fixed_action_main

    raise SystemExit(fixed_action_main(sys.argv[2:]))

from .cli import main

raise SystemExit(main())
