import os
import uuid
import jwt
import httpx
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request, HTTPException, Header
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_, and_
from fastapi.staticfiles import StaticFiles

from database import engine, Base, get_db
from models import User, Product, Order, GlobalMessage, PrivateMessage, BlockedUser

SECRET_KEY = "tradeflow_super_secret"

# ==========================================
# КЛЮЧИ ПРИЛОЖЕНИЯ ВКОНТАКТЕ (Смотри инструкцию ниже)
VK_CLIENT_ID = "ТВОЙ_АЙДИ_ПРИЛОЖЕНИЯ" 
VK_CLIENT_SECRET = "ТВОЙ_СЕКРЕТНЫЙ_КЛЮЧ"
VK_REDIRECT_URI = "http://localhost:8000/api/auth/vk/callback" # ЭТУ ССЫЛКУ МЫ ЗАМЕНИМ НА VERCEL
# ==========================================

# --- НАСТРОЙКА ПУТЕЙ ДЛЯ VERCEL ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
# ------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield

app = FastAPI(lifespan=lifespan)

# --- ПОДКЛЮЧАЕМ ПАПКУ STATIC ДЛЯ VERCEL ---
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
# -------------------------------------------

async def get_current_user(authorization: str = Header(None), db: AsyncSession = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Не авторизован")
    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        result = await db.execute(select(User).filter_by(id=int(payload.get("sub"))))
        user = result.scalar_one_or_none()
        if not user: raise HTTPException(status_code=401)
        return user
    except:
        raise HTTPException(status_code=401, detail="Недействительный токен")

# --- АВТОРИЗАЦИЯ ЧЕРЕЗ VK ID ---
@app.get("/api/auth/vk")
async def vk_login():
    # Перенаправляем пользователя на официальную страницу входа ВК
    url = f"https://oauth.vk.com/authorize?client_id={VK_CLIENT_ID}&display=page&redirect_uri={VK_REDIRECT_URI}&scope=email&response_type=code&v=5.131"
    return RedirectResponse(url)

@app.get("/api/auth/vk/callback")
async def vk_callback(code: str, db: AsyncSession = Depends(get_db)):
    # ВК возвращает нам код. Меняем его на ключ доступа (access_token)
    async with httpx.AsyncClient() as client:
        token_res = await client.get(f"https://oauth.vk.com/access_token?client_id={VK_CLIENT_ID}&client_secret={VK_CLIENT_SECRET}&redirect_uri={VK_REDIRECT_URI}&code={code}")
        token_data = token_res.json()

        if "error" in token_data:
            return RedirectResponse(url="/?error=vk_auth_failed")

        access_token = token_data["access_token"]
        vk_user_id = token_data["user_id"]

        # Получаем имя, фамилию и аватарку пользователя
        user_res = await client.get(f"https://api.vk.com/method/users.get?user_ids={vk_user_id}&fields=photo_100&access_token={access_token}&v=5.131")
        user_info = user_res.json()["response"][0]

    # Ищем пользователя в БД. Если нет - регистрируем.
    res = await db.execute(select(User).filter_by(vk_id=vk_user_id))
    user = res.scalar_one_or_none()

    if not user:
        user = User(
            vk_id=vk_user_id,
            username=f"{user_info['first_name']} {user_info['last_name']}",
            avatar_url=user_info.get('photo_100'),
            balance=5000.0 # Даем 5000р для теста
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    # Создаем наш токен для сайта
    token = jwt.encode({"sub": str(user.id), "exp": datetime.utcnow() + timedelta(days=7)}, SECRET_KEY, algorithm="HS256")
    
    # Возвращаем пользователя на главную страницу и передаем токен
    return RedirectResponse(url=f"/?token={token}")

@app.get("/api/user")
async def get_user(user: User = Depends(get_current_user)):
    return {"username": user.username, "balance": user.balance, "avatar_url": user.avatar_url}

# --- МАРКЕТПЛЕЙС ---
@app.get("/")
async def serve_frontend(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/api/products")
async def get_products(category: str = "All", subcategory: str = "Все", search: str = "", db: AsyncSession = Depends(get_db)):
    query = select(Product, User.username).join(User, Product.seller_id == User.id).filter(Product.status == "active")
    if category != "All": query = query.filter(Product.category == category)
    if subcategory and subcategory != "Все": query = query.filter(Product.subcategory == subcategory)
    if search: query = query.filter(Product.title.ilike(f"%{search}%"))

    result = await db.execute(query)
    return[{"id": p.id, "title": p.title, "description": p.description, "price": p.price, "category": p.category, "subcategory": p.subcategory, "warranty": p.has_warranty, "seller": u} for p, u in result]

@app.post("/api/sell")
async def add_product(data: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    new_product = Product(
        seller_id=user.id, category=data['category'], subcategory=data.get('subcategory', 'Разное'),
        title=data['title'], description=data['description'], has_warranty=data['warranty'],
        price=float(data['price']), account_data=data['data']
    )
    db.add(new_product)
    await db.commit()
    return {"status": "success"}

@app.post("/api/buy/{product_id}")
async def buy_product(product_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    prod_res = await db.execute(select(Product).filter_by(id=product_id))
    product = prod_res.scalar_one_or_none()
    
    if not product or product.status != "active": raise HTTPException(400, "Товар недоступен")
    if product.seller_id == user.id: raise HTTPException(400, "Вы не можете купить свой собственный товар!")
    if user.balance < product.price: raise HTTPException(400, "Недостаточно средств")

    user.balance -= product.price
    product.status = "sold"

    seller_res = await db.execute(select(User).filter_by(id=product.seller_id))
    seller = seller_res.scalar_one()
    seller.balance += product.price

    order_code = "ORD-" + uuid.uuid4().hex[:8].upper()
    new_order = Order(order_code=order_code, buyer_id=user.id, seller_id=product.seller_id, product_id=product.id, price=product.price)
    db.add(new_order)
    await db.commit()
    return {"status": "success", "order_code": order_code, "data": product.account_data}

@app.get("/api/purchases")
async def get_purchases(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    query = select(Order, Product).join(Product, Order.product_id == Product.id).filter(Order.buyer_id == user.id)
    result = await db.execute(query)
    return[{"order_code": o.order_code, "title": p.title, "price": o.price, "data": p.account_data} for o, p in result]

@app.get("/api/users/{username}")
async def get_user_profile(username: str, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(User).filter_by(username=username))
    u = res.scalar_one_or_none()
    if not u: raise HTTPException(404, "Пользователь не найден")
    
    prod_res = await db.execute(select(Product).filter_by(seller_id=u.id, status="active"))
    products = prod_res.scalars().all()
    
    return {
        "username": u.username, "avatar_url": u.avatar_url,
        "products":[{"id": p.id, "title": p.title, "price": p.price, "category": p.category, "warranty": p.has_warranty} for p in products]
    }

@app.get("/api/messages/{username}")
async def get_private_chat(username: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    target_res = await db.execute(select(User).filter_by(username=username))
    target = target_res.scalar_one_or_none()
    if not target: raise HTTPException(404)

    query = select(PrivateMessage).filter(
        or_(
            and_(PrivateMessage.sender_id == user.id, PrivateMessage.receiver_id == target.id),
            and_(PrivateMessage.sender_id == target.id, PrivateMessage.receiver_id == user.id)
        )
    ).order_by(PrivateMessage.timestamp)
    
    msgs = await db.execute(query)
    return[{"sender": user.username if m.sender_id == user.id else target.username, "text": m.text} for m in msgs.scalars().all()]

@app.post("/api/messages/{username}")
async def send_private_message(username: str, data: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    target_res = await db.execute(select(User).filter_by(username=username))
    target = target_res.scalar_one_or_none()
    if not target: raise HTTPException(404, "Пользователь не найден")

    block_check = await db.execute(select(BlockedUser).filter_by(user_id=target.id, blocked_id=user.id))
    if block_check.scalar_one_or_none(): raise HTTPException(403, "Этот пользователь заблокировал вас")

    msg = PrivateMessage(sender_id=user.id, receiver_id=target.id, text=data['text'])
    db.add(msg)
    await db.commit()
    return {"status": "ok"}

@app.post("/api/users/{username}/block")
async def block_user(username: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    target_res = await db.execute(select(User).filter_by(username=username))
    target = target_res.scalar_one_or_none()
    if not target: raise HTTPException(404)
    
    block = BlockedUser(user_id=user.id, blocked_id=target.id)
    db.add(block)
    await db.commit()
    return {"status": "blocked"}