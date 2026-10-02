"""Public invitation acceptance; token travels in POST, never in a URL path/query."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from src.dashboard.csrf import csrf_protect
from src.dashboard.auth import login_rate_limited, record_login_failure
from src.dashboard.i18n import t
from src.services import invitation_service

router = APIRouter()


@router.get('/join')
async def join(request: Request):
    return request.app.state.templates.TemplateResponse(request, 'join.html', {'error': ''})


@router.post('/join', dependencies=[Depends(csrf_protect)])
async def accept(request: Request, token: str = Form(...), password: str = Form(...)):
    error = ''
    if login_rate_limited(request):
        error = t('invite_try_later')
    else:
        try:
            await invitation_service.accept(token, password)
            return RedirectResponse('/login', status_code=303)
        except ValueError as exc:
            record_login_failure(request)
            error = t(str(exc))
    return request.app.state.templates.TemplateResponse(request, 'join.html',
        {'error': error, 'token': token if error == t('e_password_short') else ''}, status_code=400)
