import os
import uuid
import jwt
import httpx
import base64
import hashlib
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request, HTTPException, Header
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import aliased
from sqlalchemy import or_, and_, text
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from database import engine, Base, get_db
from models import User, Product, Order, PrivateMessage, BlockedUser, Review, Transaction, Report

SECRET_KEY = os.getenv("SECRET_KEY", "tradeflow_secret_key")
VK_CLIENT_ID = "54566173"
VK_CLIENT_SECRET = os.getenv("VK_CLIENT_SECRET")
VK_REDIRECT_URI = "https://wdai51.vercel.app/api/auth/vk/callback"
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        # Просто создаем таблицы. Если колонки изменились, 
        # лучше один раз вручную удалить таблицы в Supabase, чтобы они пересоздались правильно.
        await conn.run_sync(Base.metadata.create_all)
    yield

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

async def get_current_user(authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Не авторизован")
    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        user_id = int(payload.get("sub"))
        result = await db.execute(select(User).filter_by(id=user_id))
        user = result.scalar_one_or_none()
        if not user: raise HTTPException(status_code=401)
        
        if user.is_blocked:
            if user.block_until and user.block_until < datetime.utcnow():
                user.is_blocked = False
                await db.commit()
            else:
                until_str = user.block_until.strftime("%d.%m.%Y %H:%M") if user.block_until else "Навсегда"
                raise HTTPException(status_code=403, detail=f"USER_BLOCKED|{user.block_reason}|{until_str}")
        return user
    except:
        raise HTTPException(status_code=401)

class ImageUploadRequest(BaseModel):
    image_base64: str

@app.get("/api/auth/vk")
async def vk_login():
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8').rstrip('=')
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode('utf-8')).digest()).decode('utf-8').rstrip('=')
    url = f"https://id.vk.com/authorize?response_type=code&client_id={VK_CLIENT_ID}&redirect_uri={VK_REDIRECT_URI}&code_challenge={code_challenge}&code_challenge_method=S256&state=login"
    response = RedirectResponse(url)
    response.set_cookie(key="vk_code_verifier", value=code_verifier, httponly=True, max_age=600, secure=True, samesite="lax")
    return response

@app.get("/api/auth/vk/callback")
async def vk_callback(request: Request, code: str = None, device_id: str = None, db: AsyncSession = Depends(get_db)):
    try:
        code_verifier = request.cookies.get("vk_code_verifier")
        async with httpx.AsyncClient() as client:
            token_res = await client.post("https://id.vk.com/oauth2/auth", data={
                "grant_type": "authorization_code", "client_id": VK_CLIENT_ID, "client_secret": VK_CLIENT_SECRET,
                "code": code, "code_verifier": code_verifier, "redirect_uri": VK_REDIRECT_URI, "device_id": device_id or ""
            })
            token_data = token_res.json()
            access_token = token_data.get("access_token")
            user_res = await client.post("https://id.vk.com/oauth2/user_info", data={"client_id": VK_CLIENT_ID, "access_token": access_token})
            u_info = user_res.json()["user"]
            vk_id = int(u_info["user_id"])
            
        res = await db.execute(select(User).filter_by(vk_id=vk_id))
        user = res.scalar_one_or_none()
        if not user:
            username = f"{u_info.get('first_name', '')} {u_info.get('last_name', '')}".strip() or f"User{vk_id}"
            user = User(vk_id=vk_id, username=username, avatar_url=u_info.get("avatar"), balance=5000.0)
            db.add(user)
            await db.commit()
            await db.refresh(user)
            db.add(Transaction(user_id=user.id, type="topup", amount=5000.0, description="🎁 Приветственный бонус"))
            await db.commit()

        if user.username == "miellssd":
            user.role = "admin"
            await db.commit()

        token = jwt.encode({"sub": str(user.id), "exp": datetime.utcnow() + timedelta(days=7)}, SECRET_KEY, algorithm="HS256")
        return RedirectResponse(url=f"/?token={token}")
    except Exception as e:
        return RedirectResponse(url=f"/?error={str(e)}")

@app.get("/api/user")
async def get_user(user: User = Depends(get_current_user)):
    return {"username": user.username, "balance": user.balance, "avatar_url": user.avatar_url, "id": user.id, "role": user.role}

@app.post("/api/user/update")
async def update_user(data: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if data.get("username"):
        exist = await db.execute(select(User).filter(User.username == data["username"], User.id != user.id))
        if exist.scalar_one_or_none(): raise HTTPException(400, "Ник занят")
        user.username = data["username"]
    if data.get("avatar_url"): user.avatar_url = data["avatar_url"]
    await db.commit()
    return {"status": "ok"}

@app.get("/api/products")
async def get_products(category: str = "All", subcategory: str = "Все", search: str = "", db: AsyncSession = Depends(get_db)):
    query = select(Product, User.username).join(User, Product.seller_id == User.id).filter(Product.status == "active")
    if category != "All": query = query.filter(Product.category == category)
    if subcategory != "Все": query = query.filter(Product.subcategory == subcategory)
    if search: query = query.filter(Product.title.ilike(f"%{search}%"))
    res = await db.execute(query)
    return [{"id": p.id, "title": p.title, "price": p.price, "category": p.category, "subcategory": p.subcategory, "seller": u, "images": p.images.split(',') if p.images else []} for p, u in res]

@app.post("/api/sell")
async def add_product(data: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    new_p = Product(
        seller_id=user.id, category=data['category'], subcategory=data.get('subcategory', 'Разное'),
        title=data['title'], description=data['description'], has_warranty=data['warranty'],
        price=float(data['price']), account_data=data['data'], images=",".join(data.get('images', []))
    )
    db.add(new_p)
    await db.commit()
    return {"status": "ok"}

@app.post("/api/buy/{product_id}")
async def buy_product(product_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Product).filter_by(id=product_id, status="active"))
    p = res.scalar_one_or_none()
    if not p or user.balance < p.price: raise HTTPException(400, "Ошибка покупки")
    user.balance -= p.price
    seller_res = await db.execute(select(User).filter_by(id=p.seller_id))
    seller = seller_res.scalar_one()
    seller.balance += p.price
    p.status = "sold"
    order = Order(order_code="ORD-"+uuid.uuid4().hex[:8].upper(), buyer_id=user.id, seller_id=p.seller_id, product_id=p.id, price=p.price)
    db.add_all([order, Transaction(user_id=user.id, type="spend", amount=-p.price, description=f"Покупка {p.title}"), Transaction(user_id=seller.id, type="income", amount=p.price, description=f"Продажа {p.title}")])
    await db.commit()
    return {"status": "ok", "order_code": order.order_code}

@app.get("/api/users/{username}")
async def get_profile(username: str, db: AsyncSession = Depends(get_db)):
    u = (await db.execute(select(User).filter_by(username=username))).scalar_one_or_none()
    if not u: raise HTTPException(404)
    prods = (await db.execute(select(Product).filter_by(seller_id=u.id, status="active"))).scalars().all()
    return {
        "id": u.id, "username": u.username, "avatar_url": u.avatar_url,
        "products": [{"id": p.id, "title": p.title, "price": p.price, "category": p.category, "images": p.images.split(',') if p.images else []} for p in prods],
        "reviews": []
    }

@app.post("/api/reports")
async def report(data: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    db.add(Report(reporter_id=user.id, target_user_id=data.get("target_user_id"), category=data.get("category"), text=data.get("text")))
    await db.commit()
    return {"status": "ok"}

@app.get("/api/admin/reports")
async def admin_reports(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403)
    res = await db.execute(select(Report).order_by(Report.timestamp.desc()))
    return [{"category": r.category, "text": r.text, "date": r.timestamp.strftime("%d.%m %H:%M")} for r in res.scalars().all()]

@app.post("/api/admin/block")
async def admin_block(data: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403)
    target = (await db.execute(select(User).filter_by(username=data['username']))).scalar_one_or_none()
    if target:
        target.is_blocked = True
        target.block_reason = data.get('reason', 'Нарушение')
        if data.get('days'): target.block_until = datetime.utcnow() + timedelta(days=int(data['days']))
        await db.commit()
    return {"status": "ok"}

@app.delete("/api/admin/products/{pid}")
async def admin_del_p(pid: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if user.role != "admin": raise HTTPException(403)
    p = (await db.execute(select(Product).filter_by(id=pid))).scalar_one_or_none()
    if p: p.status = "deleted"
    await db.commit()
    return {"status": "ok"}

@app.get("/api/chats")
async def get_chats(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(PrivateMessage).filter(or_(PrivateMessage.sender_id == user.id, PrivateMessage.receiver_id == user.id)).order_by(PrivateMessage.timestamp.desc()))
    msgs = res.scalars().all()
    chats = {}
    for m in msgs:
        oid = m.receiver_id if m.sender_id == user.id else m.sender_id
        if oid not in chats:
            u = (await db.execute(select(User).filter_by(id=oid))).scalar_one_or_none()
            if u: chats[oid] = {"username": u.username, "last_message": m.text, "avatar_url": u.avatar_url}
    return list(chats.values())

@app.get("/api/messages/{username}")
async def get_msgs(username: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    target = (await db.execute(select(User).filter_by(username=username))).scalar_one_or_none()
    res = await db.execute(select(PrivateMessage).filter(or_(and_(PrivateMessage.sender_id==user.id, PrivateMessage.receiver_id==target.id), and_(PrivateMessage.sender_id==target.id, PrivateMessage.receiver_id==user.id))).order_by(PrivateMessage.timestamp))
    return [{"sender": user.username if m.sender_id == user.id else target.username, "text": m.text} for m in res.scalars().all()]

@app.post("/api/messages/{username}")
async def send_msg(username: str, data: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    target = (await db.execute(select(User).filter_by(username=username))).scalar_one_or_none()
    db.add(PrivateMessage(sender_id=user.id, receiver_id=target.id, text=data['text']))
    await db.commit()
    return {"status": "ok"}

@app.get("/api/finances")
async def get_fin(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Transaction).filter_by(user_id=user.id).order_by(Transaction.timestamp.desc()))
    return [{"description": t.description, "amount": t.amount, "date": t.timestamp.strftime("%d.%m %H:%M")} for t in res.scalars().all()]

@app.get("/api/purchases")
async def get_pur(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Order, Product).join(Product).filter(Order.buyer_id == user.id))
    return [{"title": p.title, "data": p.account_data} for o, p in res]

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/upload_image")
async def upload(data: ImageUploadRequest):
    try:
        img_bytes = base64.b64decode(data.image_base64.split(",")[1] if "," in data.image_base64 else data.image_base64)
        name = f"{uuid.uuid4().hex}.jpg"
        url = f"{SUPABASE_URL.split('/rest/v1')[0]}/storage/v1/object/tradeflow/{name}"
        async with httpx.AsyncClient() as client:
            res = await client.post(url, content=img_bytes, headers={"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY, "Content-Type": "image/jpeg"})
            if res.status_code in (200, 201):
                return {"url": f"{SUPABASE_URL.split('/rest/v1')[0]}/storage/v1/object/public/tradeflow/{name}"}
        raise HTTPException(400)
    except Exception as e: raise HTTPException(500, str(e))