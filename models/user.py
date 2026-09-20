from pydantic import BaseModel, EmailStr


class SendOtpRequest(BaseModel):
    email: EmailStr


class VerifyOtpRequest(BaseModel):
    email: EmailStr
    otp: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    is_new_user: bool = False


class UsernameUpdate(BaseModel):
    username: str
