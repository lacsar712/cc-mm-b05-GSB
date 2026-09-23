from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import BASE_ALERT_PCT, classify


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class Reading(Base):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ThresholdHistory(Base):
    """报警线临时抬高履历，只追加，不修改不覆盖。"""

    __tablename__ = "threshold_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    threshold_pct: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    set_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class ThresholdIn(BaseModel):
    threshold_pct: float = Field(gt=BASE_ALERT_PCT, description="临时报警线，必须高于百分之一")
    expires_in_seconds: int | None = Field(default=None, gt=0, le=24 * 3600)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _one_expiry(self):
        if self.expires_in_seconds is None and self.expires_at is None:
            raise ValueError("必须给出失效时刻 expires_at 或有效期 expires_in_seconds")
        return self


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    username = payload.get("sub")
    if username not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": username, "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅瓦斯检查员可操作")
    return user


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


def active_threshold(db: Session, now: datetime) -> ThresholdHistory | None:
    """当前生效的临时线：取履历最新一条；它未失效即生效，否则回落到百分之一。

    履历只追加：每次抬高写入新记录，旧记录原样保留但不复活——后续抬高
    设定的是当前线，最新一条失效即回到百分之一，不会回退到更早的临时线。
    在 Python 侧归一化时区后比较，避免不同驱动时间类型差异。
    """
    row = db.query(ThresholdHistory).order_by(ThresholdHistory.id.desc()).first()
    if row is not None and as_utc(row.expires_at) > now:
        return row
    return None


def as_utc(dt: datetime) -> datetime:
    # SQLite 等驱动可能取回无时区时间，统一按 UTC 处理。
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def threshold_dict(row: ThresholdHistory, now: datetime, active: bool) -> dict:
    expires_at = as_utc(row.expires_at)
    created_at = as_utc(row.created_at)
    return {
        "id": row.id,
        "threshold_pct": row.threshold_pct,
        "expires_at": expires_at.isoformat(),
        "set_by": row.set_by,
        "created_at": created_at.isoformat(),
        "expired": expires_at <= now,
        "active": active,
    }


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(Reading).count() == 0:
            now = datetime.now(timezone.utc)
            for site, ch4 in (("东翼-12", 0.35), ("回风巷", 1.4)):
                level, note = classify(ch4)
                db.add(
                    Reading(
                        site=site,
                        ch4_pct=ch4,
                        level=level,
                        note=note,
                        created_by="gasman",
                        created_at=now,
                    )
                )
            db.commit()
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "mine-methane-shift"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": body.username.strip(), "role": user["role"], "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Reading).order_by(Reading.id.desc()).all()
        return [
            {
                "id": r.id,
                "site": r.site,
                "ch4_pct": r.ch4_pct,
                "level": r.level,
                "note": r.note,
                "created_by": r.created_by,
            }
            for r in rows
        ]
    finally:
        db.close()


@app.post("/api/readings", status_code=201)
async def create_reading(body: ReadingIn, user: dict = Depends(require_writer)):
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        temp = active_threshold(db, now)
        alert_pct = temp.threshold_pct if temp else BASE_ALERT_PCT
        level, note = classify(body.ch4_pct, alert_pct)
        row = Reading(
            site=body.site.strip(),
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            created_by=user["username"],
            created_at=now,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        payload = {
            "id": row.id,
            "site": row.site,
            "ch4_pct": row.ch4_pct,
            "level": row.level,
            "note": row.note,
            "alert_pct": alert_pct,
        }
        is_alarm = row.level == "报警"
    finally:
        db.close()
    # 推送规则跟着判定走：只有报警才推送；临时线生效期间按临时线判定。
    if is_alarm:
        dead = []
        for ws in list(sockets):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            sockets.discard(ws)
    return payload


@app.get("/api/thresholds/current")
def current_threshold(_user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        row = active_threshold(db, now)
        if row is None:
            return {"active": False, "threshold_pct": BASE_ALERT_PCT, "expires_at": None}
        return {
            "active": True,
            "threshold_pct": row.threshold_pct,
            "expires_at": row.expires_at.isoformat(),
            "set_by": row.set_by,
        }
    finally:
        db.close()


@app.get("/api/thresholds/history")
def threshold_history(_user: dict = Depends(current_user)):
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        rows = db.query(ThresholdHistory).order_by(ThresholdHistory.id.desc()).all()
        active_id = active_threshold(db, now).id if active_threshold(db, now) else None
        return [threshold_dict(r, now, active=r.id == active_id) for r in rows]
    finally:
        db.close()


@app.post("/api/thresholds", status_code=201)
def raise_threshold(body: ThresholdIn, user: dict = Depends(require_writer)):
    now = datetime.now(timezone.utc)
    if body.expires_in_seconds is not None:
        expires_at = now + timedelta(seconds=body.expires_in_seconds)
    else:
        expires_at = body.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        expires_at = expires_at.astimezone(timezone.utc)
    if expires_at <= now:
        raise HTTPException(status_code=422, detail="失效时刻必须晚于当前时间")
    db = SessionLocal()
    try:
        row = ThresholdHistory(
            threshold_pct=body.threshold_pct,
            expires_at=expires_at,
            set_by=user["username"],
            created_at=now,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        # 旧记录原样保留，不更新不覆盖；最新未失效记录即为当前临时线。
        return threshold_dict(row, now, active=True)
    finally:
        db.close()


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
