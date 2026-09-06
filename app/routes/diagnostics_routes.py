from fastapi import APIRouter, Depends, Request

from ..templating import templates
from ..auth import require_operator
from ..modules import diagnostics

router = APIRouter(dependencies=[Depends(require_operator)])


@router.get("/diagnostics")
def diagnostics_index(request: Request):
    return templates.TemplateResponse("diagnostics/index.html", {"request": request, **diagnostics.run_diagnostics()})
