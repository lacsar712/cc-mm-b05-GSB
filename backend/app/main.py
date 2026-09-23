from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import DEFAULT_THRESHOLD, classify


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


class ThresholdRaise(Base):
    """临时抬线履历，只追加；旧记录永不更新或删除。"""

    __tablename__ = "threshold_raises"
    id: Mapped[int] = mapped_column(primary_key=True)
    threshold: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class ThresholdRaiseIn(BaseModel):
    threshold: float = Field(gt=DEFAULT_THRESHOLD)
    # 有效期（秒），检查员抬线时给一个较短的失效时刻
    valid_seconds: int = Field(gt=0, le=24 * 3600)


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


def active_threshold(db: Session, now: datetime | None = None) -> tuple[float, ThresholdRaise | None]:
    """返回当前生效的报警线；没有未失效的临时线时回到百分之一。

    履历只追加，失效记录原样保留，不覆盖、不删除。
    同一时刻有多条有效记录（不同抬线失效时刻不同）时取最新一条。
    """
    now = now or datetime.now(timezone.utc)
    raise_ = (
        db.query(ThresholdRaise)
        .filter(ThresholdRaise.expires_at > now)
        .order_by(ThresholdRaise.id.desc())
        .first()
    )
    if raise_ is None:
        return DEFAULT_THRESHOLD, None
    return raise_.threshold, raise_


def _iso(dt: datetime) -> str:
    # sqlite 读回的时间不带时区，统一按 UTC 输出（postgres 下本就来去一致）
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def raise_to_dict(r: ThresholdRaise) -> dict:
    return {
        "id": r.id,
        "threshold": r.threshold,
        "expires_at": _iso(r.expires_at),
        "created_by": r.created_by,
        "created_at": _iso(r.created_at),
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
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        threshold, raise_ = active_threshold(db, now)
        level, note = classify(body.ch4_pct, threshold)
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
            "threshold": threshold,
        }
        is_alarm = row.level == "报警"
    finally:
        db.close()
    # 推送规则跟着当前报警线走：只有判定为报警才推送；
    # 临时线生效期间低于临时线的正常读数不推送报警。
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


@app.get("/api/thresholds")
def list_thresholds(_user: dict = Depends(current_user)):
    """当前线 + 完整抬线履历。旁观账号只读。"""
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        threshold, raise_ = active_threshold(db, now)
        history = db.query(ThresholdRaise).order_by(ThresholdRaise.id.desc()).all()
        return {
            "current": threshold,
            "default": DEFAULT_THRESHOLD,
            "active_raise": raise_to_dict(raise_) if raise_ else None,
            "server_time": now.isoformat(),
            "history": [raise_to_dict(r) for r in history],
        }
    finally:
        db.close()


@app.post("/api/thresholds", status_code=201)
def raise_threshold(body: ThresholdRaiseIn, user: dict = Depends(require_writer)):
    """检查员临时抬高报警线并设定失效时刻；每次抬高追加一条履历。"""
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        row = ThresholdRaise(
            threshold=body.threshold,
            expires_at=now + timedelta(seconds=body.valid_seconds),
            created_by=user["username"],
            created_at=now,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return raise_to_dict(row)
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
