from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, Boolean
from datetime import datetime
from database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    vk_id = Column(Integer, unique=True, index=True) # Привязка к ВК
    username = Column(String)                        # Имя и Фамилия из ВК
    avatar_url = Column(String, nullable=True)       # Аватарка из ВК
    balance = Column(Float, default=0.0)

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    seller_id = Column(Integer, ForeignKey("users.id"))
    category = Column(String, default="Другое")
    subcategory = Column(String, default="Разное")
    title = Column(String)
    description = Column(String, default="")
    has_warranty = Column(Boolean, default=False)
    price = Column(Float)
    account_data = Column(String)
    status = Column(String, default="active")

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    order_code = Column(String, unique=True)
    buyer_id = Column(Integer, ForeignKey("users.id"))
    seller_id = Column(Integer, ForeignKey("users.id"))
    product_id = Column(Integer, ForeignKey("products.id"))
    price = Column(Float)
    status = Column(String, default="paid")

class PrivateMessage(Base):
    __tablename__ = "private_messages"
    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"))
    receiver_id = Column(Integer, ForeignKey("users.id"))
    text = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)

class BlockedUser(Base):
    __tablename__ = "blocked_users"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    blocked_id = Column(Integer, ForeignKey("users.id"))

class GlobalMessage(Base):
    __tablename__ = "global_chat"
    id = Column(Integer, primary_key=True, index=True)
    sender_id = Column(Integer, ForeignKey("users.id"))
    sender_name = Column(String)
    text = Column(String)
    timestamp = Column(DateTime, default=datetime.utcnow)