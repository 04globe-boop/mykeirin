"""Bound capture finishing around the existing T-5 prediction windows."""
from datetime import timedelta

HANDOFF_LOOKAHEAD_SECONDS=120
MAX_HANDOFF_EXTENSION_SECONDS=180

def prediction_handoff_end(nominal_end,current_end,races):
    """Move the end forward through nearby windows, with a fixed planning cap."""
    end=current_end
    limit=nominal_end+timedelta(seconds=MAX_HANDOFF_EXTENSION_SECONDS)
    while True:
        cutoffs=[race.close-timedelta(minutes=5) for race in races]
        nearby=[cutoff for cutoff in cutoffs if end<cutoff and
                cutoff-timedelta(seconds=20)<=end+timedelta(seconds=HANDOFF_LOOKAHEAD_SECONDS)]
        if not nearby:return end,False
        requested=max(nearby)+timedelta(seconds=1)
        bounded=min(requested,limit)
        if bounded<=end:return end,requested>limit
        end=bounded
        if requested>limit:return end,True
