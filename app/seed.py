"""Seeds one demo business — Boutique Teranga (Alimentation) — for local dev.

Ported from the old C# build's SeedData.cs, which was itself validated
against the Charte Conversationnelle and the merchant's actual product list.
Run with: python -m app.seed
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import FAQ, Business, Contact, Product, Promotion

TERANGA_WHATSAPP_NUMBER = "15551716750"  # Meta sandbox test number for this project's WhatsApp app
TERANGA_OWNER_WHATSAPP_NUMBER = "221770009999"  # Moussa's personal number -> merchant admin access


async def seed() -> None:
    async with SessionLocal() as db:
        existing = await db.scalar(select(Business).where(Business.whatsapp_number == TERANGA_WHATSAPP_NUMBER))
        if existing:
            print("Boutique Teranga already seeded, skipping.")
            return

        business = Business(
            name="Boutique Teranga",
            owner_name="Moussa Diop",
            whatsapp_number=TERANGA_WHATSAPP_NUMBER,
            owner_whatsapp_number=TERANGA_OWNER_WHATSAPP_NUMBER,
            address="Médina, Rue 10, Dakar",
            welcome_message="Dall leen ak jàmm! Bienvenue chez Boutique Teranga. Comment puis-je vous aider aujourd'hui ?",
            away_message="Merci pour votre message. Nous sommes actuellement fermés et vous répondrons dès notre retour. -- Jaaykat bi",
        )
        db.add(business)
        await db.flush()

        products = [
            Product(
                business_id=business.id,
                name="Riz parfumé 5 kg",
                category="Alimentation",
                price_xof=4500,
                track_inventory=True,
                quantity_in_stock=50,
                low_stock_threshold=10,
            ),
            Product(
                business_id=business.id,
                name="Huile d'arachide 1L",
                category="Alimentation",
                price_xof=2500,
                track_inventory=True,
                quantity_in_stock=30,
                low_stock_threshold=5,
            ),
            Product(
                business_id=business.id,
                name="Sucre en poudre 1 kg",
                category="Alimentation",
                price_xof=800,
                track_inventory=True,
                quantity_in_stock=100,
                low_stock_threshold=20,
            ),
            Product(
                business_id=business.id,
                name="Lait en poudre Nido 400g",
                category="Alimentation",
                price_xof=3200,
                track_inventory=True,
                quantity_in_stock=25,
                low_stock_threshold=5,
            ),
        ]
        db.add_all(products)
        await db.flush()

        contact = Contact(
            business_id=business.id,
            wa_id="221770001111",
            phone_number="221770001111",
            display_name="Amadou Fall",
            last_contact_at=datetime.now(UTC),
            total_orders=3,
            total_spent_xof=15500,
        )
        db.add(contact)

        faqs = [
            FAQ(
                business_id=business.id,
                question="Quels sont vos horaires d'ouverture ?",
                answer="Notre boutique est ouverte du lundi au samedi de 8h à 20h, et le dimanche de 9h à 14h.",
                keywords="horaire,heure,ouverture,fermé,ouvert",
                category="general",
            ),
            FAQ(
                business_id=business.id,
                question="Proposez-vous la livraison ?",
                answer="Oui, nous livrons dans tout Dakar. Les frais de livraison varient selon la zone (500 à 1500 FCFA).",
                keywords="livraison,livrer,transport,frais,zone",
                category="delivery",
            ),
            FAQ(
                business_id=business.id,
                question="Quels modes de paiement acceptez-vous ?",
                answer="Nous acceptons Wave, Orange Money et le paiement en espèces à la livraison.",
                keywords="paiement,payer,wave,orange,money,espèce,cash",
                category="payment",
            ),
            FAQ(
                business_id=business.id,
                question="Puis-je annuler ma commande ?",
                answer="Oui, vous pouvez annuler votre commande tant qu'elle n'a pas été expédiée. Contactez-nous avec votre référence de commande.",
                keywords="annuler,annulation,rembourser,remboursement,commande",
                category="orders",
            ),
        ]
        db.add_all(faqs)

        # A sample active promotion so the catalog Q&A / order flow has a
        # discounted product to exercise during manual testing.
        promo = Promotion(
            business_id=business.id,
            product_id=products[1].id,  # Huile d'arachide 1L
            title="Promo huile -10%",
            discount_percent=10,
            duration_days=7,
            end_date=datetime.now(UTC) + timedelta(days=7),
        )
        db.add(promo)

        await db.commit()
        print(f"Seeded Boutique Teranga ({business.id}) with {len(products)} products, "
              f"{len(faqs)} FAQs, 1 contact, 1 active promotion.")


if __name__ == "__main__":
    asyncio.run(seed())
