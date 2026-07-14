import os

# ── auth.py - add name to TokenResponse ──────────────────────────────────────
auth_path = "app/api/routes/auth.py"
with open(auth_path) as f:
    auth_content = f.read()

warnings = []

old_token_response = """class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    role: str
    user_id: str"""

new_token_response = """class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    role: str
    user_id: str
    name: str = ""   # passenger/driver name for personalised UI"""

if old_token_response in auth_content:
    auth_content = auth_content.replace(old_token_response, new_token_response, 1)
else:
    warnings.append("TokenResponse anchor not found in auth.py")

# Add name to the return statement in verify_otp_endpoint
old_return = """    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        role=user.role.value,
        user_id=str(user.id),
    )"""

new_return = """    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        role=user.role.value,
        user_id=str(user.id),
        name=user.name,
    )"""

if old_return in auth_content:
    auth_content = auth_content.replace(old_return, new_return, 1)
else:
    warnings.append("TokenResponse return anchor not found in auth.py")

with open(auth_path, "w") as f:
    f.write(auth_content)

# ── routes.py - pass user_name to process_message ────────────────────────────
routes_path = "app/api/routes/routes.py"
with open(routes_path) as f:
    routes_content = f.read()

old_process = """    response = await agent_service.process_message(
        user_id=current_user.id,
        user_message=body.message.strip(),
        db=db,
    )"""

new_process = """    response = await agent_service.process_message(
        user_id=current_user.id,
        user_message=body.message.strip(),
        db=db,
        user_name=current_user.name,
    )"""

if old_process in routes_content:
    routes_content = routes_content.replace(old_process, new_process, 1)
else:
    warnings.append("process_message call anchor not found in routes.py")

with open(routes_path, "w") as f:
    f.write(routes_content)

if warnings:
    print("WARNINGS:")
    for w in warnings:
        print(f"  - {w}")
else:
    print("Done - auth.py: name in TokenResponse; routes.py: user_name passed to agent")
