"""What the conversation engine hands back to the webhook layer for sending.
A `BotReply` carries plain text plus, optionally, WhatsApp interactive
elements (buttons for <=3 choices, a list for more) — the message_handler
picks the right WhatsAppClient call based on which fields are set.

Button/list titles double as the text the routing logic matches on (a tap
delivers the tapped title back as the inbound message text, same as if the
user had typed it) — see intents.py / customer_flows.py / merchant_flows.py
for the keyword matches each title below is chosen to hit."""

from dataclasses import dataclass


@dataclass
class BotReply:
    text: str
    buttons: list[tuple[str, str]] | None = None  # [(id, title)], max 3
    list_button_text: str | None = None
    list_sections: list[tuple[str, list[tuple[str, str, str]]]] | None = None
    # [(section_title, [(row_id, row_title, row_description)])]
    merchant_notification: str | None = None
    # Set when the customer-side reply should also trigger a separate WhatsApp
    # message to the merchant (e.g. a new pending order needs their
    # attention) — message_handler.py sends this to business.owner_whatsapp_number
    # once it's done sending the main `text` reply to whoever it's actually
    # replying to.


# Appended to every list-based prompt (product pickers, and any bounded
# choice that's already at WhatsApp's 3-button cap) so there's always a
# tappable way out, not just the typed "annuler" keyword.
ANNULER_SECTION: tuple[str, list[tuple[str, str, str]]] = (
    "Autre", [("annuler", "Annuler", "Annuler l'opération en cours")]
)

MERCHANT_MENU_SECTIONS: list[tuple[str, list[tuple[str, str, str]]]] = [
    (
        "Actions",
        [
            ("voir_catalogue", "Voir le catalogue", "Voir tous les produits par catégorie"),
            ("ajouter_produit", "Ajouter un produit", "Ajouter un nouvel article au catalogue"),
            ("modifier_produit", "Modifier un produit", "Changer prix, stock ou disponibilité"),
            ("supprimer_produit", "Supprimer un produit", "Retirer un article du catalogue"),
            ("mes_ventes", "Mes ventes", "Voir le résumé des ventes"),
            ("lancer_promotion", "Lancer une promotion", "Créer une réduction temporaire"),
            ("arreter_promotion", "Arrêter une promotion", "Mettre fin à une promotion avant son échéance"),
            ("mes_commandes", "Mes commandes", "Voir les commandes en attente"),
            ("messages_en_attente", "Messages en attente", "Conversations en attente d'une réponse humaine"),
        ],
    )
]


def with_merchant_menu(text: str) -> BotReply:
    return BotReply(text=text, list_button_text="Menu", list_sections=MERCHANT_MENU_SECTIONS)


def with_customer_menu(text: str, merchant_notification: str | None = None) -> BotReply:
    return BotReply(
        text=text,
        buttons=[
            ("voir_catalogue", "Voir catalogue"),
            ("voir_promotions", "Promotions"),
            ("commander_produit", "Commander"),
        ],
        merchant_notification=merchant_notification,
    )


def with_cancel_button(text: str) -> BotReply:
    """For free-text prompts mid-flow: a lone escape-hatch button alongside
    the text prompt, so leaving doesn't require remembering to type 'annuler'."""
    return BotReply(text=text, buttons=[("annuler", "Annuler")])
