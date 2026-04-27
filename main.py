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
from sqlalchemy import or_, and_
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from database import engine, Base, get_db
from models import User, Product, Order, PrivateMessage, BlockedUser, Review

SECRET_KEY = "tradeflow_super_secret"

# ==========================================
# КЛЮЧИ ПРИЛОЖЕНИЯ ВКОНТАКТЕ
# ==========================================
VK_CLIENT_ID = "54566173" 
VK_CLIENT_SECRET = os.getenv("VK_CLIENT_SECRET") # Берется из Vercel!
VK_REDIRECT_URI = "https://wdai51.vercel.app/api/auth/vk/callback"

# --- НАСТРОЙКА ПУТЕЙ ДЛЯ VERCEL ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, checkfirst=True)
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
        result = await db.execute(select(User).filter_by(id=int(payload.get("sub"))))
        user = result.scalar_one_or_none()
        if not user: raise HTTPException(status_code=401)
        return user
    except:
        raise HTTPException(status_code=401, detail="Недействительный токен")

class VKTokenRequest(BaseModel):
    access_token: str

# ==========================================
# --- БРОНЕБОЙНАЯ АВТОРИЗАЦИЯ VK ID (PKCE) ---
# ==========================================

@app.get("/api/auth/vk")
async def vk_login():
    # Генерируем правильные ключи для нового API (это убирает Security Error!)
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8').rstrip('=')
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode('utf-8')).digest()).decode('utf-8').rstrip('=')
    
    # Используем правильную новую ссылку id.vk.com
    url = f"https://id.vk.com/authorize?response_type=code&client_id={VK_CLIENT_ID}&redirect_uri={VK_REDIRECT_URI}&code_challenge={code_challenge}&code_challenge_method=S256&state=login"
    
    response = RedirectResponse(url)
    response.set_cookie(key="vk_code_verifier", value=code_verifier, httponly=True, max_age=600, secure=True, samesite="lax")
    return response

@app.get("/api/auth/vk/callback")
async def vk_callback(request: Request, code: str = None, device_id: str = None, state: str = None, error: str = None, error_description: str = None, db: AsyncSession = Depends(get_db)):
    try:
        if error:
            return RedirectResponse(url=f"/?error={error_description}")
        if not code:
            return RedirectResponse(url="/?error=ВК_не_прислал_код_подтверждения")
            
        code_verifier = request.cookies.get("vk_code_verifier")
        if not code_verifier:
            return RedirectResponse(url="/?error=Сессия_устарела_попробуйте_еще_раз")

        async with httpx.AsyncClient() as client:
            # 1. Получаем токен через новый API VK ID
            token_data_req = {
                "grant_type": "authorization_code",
                "client_id": VK_CLIENT_ID,
                "client_secret": VK_CLIENT_SECRET or "",
                "code": code,
                "code_verifier": code_verifier,
                "device_id": device_id or "",
                "redirect_uri": VK_REDIRECT_URI,
                "state": state or "login"
            }
            
            token_res = await client.post("https://id.vk.com/oauth2/auth", data=token_data_req, headers={"Content-Type": "application/x-www-form-urlencoded"})
            token_data = token_res.json()
            
            if "error" in token_data:
                err_msg = token_data.get('error_description', token_data.get('error'))
                return RedirectResponse(url=f"/?error=Ошибка_токена_{err_msg}")

            access_token = token_data.get("access_token")
            
            # 2. Получение данных пользователя
            user_res = await client.post("https://id.vk.com/oauth2/user_info", data={
                "client_id": VK_CLIENT_ID,
                "access_token": access_token
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})
            user_data = user_res.json()
            
            if "user" in user_data:
                u_info = user_data["user"]
                vk_user_id = int(u_info.get("user_id"))
                first_name = u_info.get("first_name", "")
                last_name = u_info.get("last_name", "")
                avatar = u_info.get("avatar", "")
            else:
                # Резервный метод (если новый API не отдал данные)
                old_res = await client.get(f"https://api.vk.com/method/users.get?fields=photo_100&access_token={access_token}&v=5.131")
                old_data = old_res.json()
                if "response" in old_data and len(old_data["response"]) > 0:
                    u_info = old_data["response"][0]
                    vk_user_id = int(u_info["id"])
                    first_name = u_info.get("first_name", "")
                    last_name = u_info.get("last_name", "")
                    avatar = u_info.get("photo_100", "")
                else:
                    return RedirectResponse(url="/?error=Не_удалось_получить_профиль")

        # 3. Сохраняем пользователя в базу
        res = await db.execute(select(User).filter_by(vk_id=vk_user_id))
        user = res.scalar_one_or_none()

        if not user:
            user = User(
                vk_id=vk_user_id,
                username=f"{first_name} {last_name}".strip() or f"User{vk_user_id}",
                avatar_url=avatar,
                balance=5000.0
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)

        # 4. Выдаем токен для сайта
        token = jwt.encode({"sub": str(user.id), "exp": datetime.utcnow() + timedelta(days=7)}, SECRET_KEY, algorithm="HS256")
        
        response = RedirectResponse(url=f"/?token={token}")
        response.delete_cookie("vk_code_verifier")
        return response
        
    except Exception as e:
        # Теперь вместо 500 ошибки юзер увидит красивую всплывашку на сайте с причиной
        error_msg = str(e).replace(" ", "_")
        return RedirectResponse(url=f"/?error=Системная_ошибка_{error_msg}")

@app.post("/api/auth/vk/token")
async def vk_token_auth(data: VKTokenRequest, db: AsyncSession = Depends(get_db)):
    try:
        async with httpx.AsyncClient() as client:
            user_res = await client.post("https://id.vk.com/oauth2/user_info", data={
                "client_id": VK_CLIENT_ID,
                "access_token": data.access_token
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})
            user_data = user_res.json()
            
            if "user" in user_data:
                u_info = user_data["user"]
                vk_user_id = int(u_info.get("user_id"))
                first_name = u_info.get("first_name", "")
                last_name = u_info.get("last_name", "")
                avatar = u_info.get("avatar", "")
            else:
                old_res = await client.get(f"https://api.vk.com/method/users.get?fields=photo_100&access_token={data.access_token}&v=5.131")
                old_data = old_res.json()
                if "response" in old_data and len(old_data["response"]) > 0:
                    u_info = old_data["response"][0]
                    vk_user_id = int(u_info["id"])
                    first_name = u_info.get("first_name", "")
                    last_name = u_info.get("last_name", "")
                    avatar = u_info.get("photo_100", "")
                else:
                    return JSONResponse(status_code=400, content={"detail": "Не удалось получить профиль ВК"})
                    
        res = await db.execute(select(User).filter_by(vk_id=vk_user_id))
        user = res.scalar_one_or_none()

        if not user:
            user = User(
                vk_id=vk_user_id,
                username=f"{first_name} {last_name}".strip() or f"User{vk_user_id}",
                avatar_url=avatar,
                balance=5000.0
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)

        token = jwt.encode({"sub": str(user.id), "exp": datetime.utcnow() + timedelta(days=7)}, SECRET_KEY, algorithm="HS256")
        return {"token": token}
        
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"Ошибка сервера: {str(e)}"})


# --- ОСТАЛЬНЫЕ ЭНДПОИНТЫ ---

@app.get("/api/user")
async def get_user(user: User = Depends(get_current_user)):
    return {"username": user.username, "balance": user.balance, "avatar_url": user.avatar_url, "id": user.id}

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
    return[{"id": p.id, "title": p.title, "description": p.description, "price": p.price, "category": p.category, "subcategory": p.subcategory, "warranty": p.has_warranty, "seller": u, "images": p.images.split(',') if p.images else []} for p, u in result]

@app.post("/api/sell")
async def add_product(data: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    images_str = ",".join(data.get('images', [])[:8])
    new_product = Product(
        seller_id=user.id, category=data['category'], subcategory=data.get('subcategory', 'Разное'),
        title=data['title'], description=data['description'], has_warranty=data['warranty'],
        price=float(data['price']), account_data=data['data'], images=images_str
    )
    db.add(new_product)
    await db.commit()
    return {"status": "success"}

@app.post("/api/buy/{product_id}")
async def buy_product(product_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    prod_res = await db.execute(select(Product).filter_by(id=product_id))
    product = prod_res.scalar_one_or_none()
    if not product or product.status != "active": raise HTTPException(400, "Товар недоступен")
    if product.seller_id == user.id: raise HTTPException(400, "Вы не можете купить свой собственный товар")
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
    return[{"order_code": o.order_code, "title": p.title, "price": o.price, "data": p.account_data, "product_id": p.id, "seller_id": p.seller_id} for o, p in result]

@app.get("/api/users/{username}")
async def get_user_profile(username: str, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(User).filter_by(username=username))
    u = res.scalar_one_or_none()
    if not u: raise HTTPException(404, "Пользователь не найден")
    
    prod_res = await db.execute(select(Product).filter_by(seller_id=u.id, status="active"))
    products = prod_res.scalars().all()
    
    rev_res = await db.execute(select(Review, User.username).join(User, Review.buyer_id == User.id).filter(Review.seller_id == u.id))
    reviews =[{"id": r.id, "buyer": buyer_name, "text": r.text, "reply": r.seller_reply, "date": r.timestamp.strftime("%d.%m.%Y")} for r, buyer_name in rev_res]
    
    return {
        "username": u.username, "avatar_url": u.avatar_url, "id": u.id,
        "products":[{"id": p.id, "title": p.title, "price": p.price, "category": p.category, "images": p.images.split(',') if p.images else []} for p in products],
        "reviews": reviews
    }

@app.post("/api/reviews")
async def post_review(data: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rev_res = await db.execute(select(Review).filter_by(buyer_id=user.id, product_id=data['product_id']))
    review = rev_res.scalar_one_or_none()
    action = "оставил"
    if review:
        review.text = data['text']
        action = "изменил"
    else:
        review = Review(product_id=data['product_id'], buyer_id=user.id, seller_id=data['seller_id'], text=data['text'])
        db.add(review)
        
    sys_msg = PrivateMessage(sender_id=user.id, receiver_id=data['seller_id'], text=f"📢 Покупатель {action} отзыв: «{data['text']}»")
    db.add(sys_msg)
    await db.commit()
    return {"status": "success"}

@app.post("/api/reviews/{review_id}/reply")
async def reply_review(review_id: int, data: dict, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Review).filter_by(id=review_id, seller_id=user.id))
    review = res.scalar_one_or_none()
    if not review: raise HTTPException(403, "Отзыв не найден или вы не продавец")
    review.seller_reply = data['reply']
    await db.commit()
    return {"status": "success"}

@app.get("/api/messages/{username}")
async def get_private_chat(username: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    target_res = await db.execute(select(User).filter_by(username=username))
    target = target_res.scalar_one_or_none()
    if not target: raise HTTPException(404)
    query = select(PrivateMessage).filter(or_(and_(PrivateMessage.sender_id == user.id, PrivateMessage.receiver_id == target.id), and_(PrivateMessage.sender_id == target.id, PrivateMessage.receiver_id == user.id))).order_by(PrivateMessage.timestamp)
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