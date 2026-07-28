"""Keyword-based intent detection — ported as-is from the old C# `DetectIntent`
(simple substring matching, French + a few Wolof words). It's not AI, but it
was already reliable, and it decides which deterministic flow to start."""

import enum


class Intent(str, enum.Enum):
    GREETING = "greeting"
    GOODBYE = "goodbye"
    VOIR_CATALOGUE = "voir_catalogue"
    VOIR_PROMOTIONS = "voir_promotions"
    COMMANDER_PRODUIT = "commander_produit"
    ANNULER_COMMANDE = "annuler_commande"
    STATUT_COMMANDE = "statut_commande"
    PARLER_A_QUELQUUN = "parler_a_quelquun"
    AJOUTER_PRODUIT = "ajouter_produit"
    MODIFIER_PRODUIT = "modifier_produit"
    SUPPRIMER_PRODUIT = "supprimer_produit"
    CONSULTER_VENTES = "consulter_ventes"
    LANCER_PROMOTION = "lancer_promotion"
    ARRETER_PROMOTION = "arreter_promotion"
    VOIR_COMMANDES = "voir_commandes"
    MESSAGES_EN_ATTENTE = "messages_en_attente"
    DEMANDER_RETOUR = "demander_retour"
    UNKNOWN = "unknown"


_COMMON_KEYWORDS: dict[Intent, list[str]] = {
    Intent.GREETING: [
        "bonjour", "salut", "bonsoir", "hello", "hi", "salam", "assalamu",
        "dall leen", "na nga def", "jàmm", "waaw",
    ],
    Intent.GOODBYE: ["au revoir", "à bientôt", "bye", "merci beaucoup et au revoir"],
}

_CUSTOMER_KEYWORDS: dict[Intent, list[str]] = {
    # Most specific intents first — COMMANDER_PRODUIT's keywords are broad
    # enough ("commander" etc.) that a generic word checked late would never
    # get a chance to win against a more specific phrase checked earlier.
    Intent.DEMANDER_RETOUR: [
        "retourner ma commande", "faire un retour", "demander un remboursement",
        "je veux un remboursement", "renvoyer ma commande",
    ],
    Intent.ANNULER_COMMANDE: ["annuler", "annulation"],
    Intent.STATUT_COMMANDE: ["statut", "où en est", "suivi de ma commande"],
    Intent.PARLER_A_QUELQUUN: ["parler à quelqu'un", "un humain", "une vraie personne", "assistance humaine"],
    Intent.VOIR_CATALOGUE: ["catalogue", "voir les produits", "vos produits", "qu'est-ce que vous avez", "liste des produits"],
    Intent.VOIR_PROMOTIONS: ["promotion", "promo", "offre", "réduction", "solde"],
    Intent.COMMANDER_PRODUIT: ["commander", "acheter", "passer commande", "je voudrais commander", "prendre une commande"],
}

_MERCHANT_KEYWORDS: dict[Intent, list[str]] = {
    Intent.VOIR_CATALOGUE: ["voir le catalogue", "mon catalogue", "catalogue"],
    Intent.AJOUTER_PRODUIT: ["ajouter un produit", "ajouter produit", "nouveau produit"],
    Intent.MODIFIER_PRODUIT: ["modifier un produit", "modifier produit", "changer le prix"],
    Intent.SUPPRIMER_PRODUIT: ["supprimer un produit", "supprimer produit", "retirer un produit"],
    Intent.CONSULTER_VENTES: ["mes ventes", "voir mes ventes", "statistiques", "chiffre d'affaires"],
    Intent.LANCER_PROMOTION: ["lancer une promotion", "lancer promotion", "créer une promotion"],
    Intent.ARRETER_PROMOTION: ["arrêter une promotion", "arreter une promotion", "terminer une promotion", "stopper une promotion"],
    Intent.VOIR_COMMANDES: ["mes commandes", "commandes en attente", "voir les commandes"],
    Intent.MESSAGES_EN_ATTENTE: ["messages en attente", "conversations en attente"],
}

# Price questions are redirected to the catalog rather than treated as a
# distinct intent (mirrors the old code).
_PRICE_KEYWORDS = ["prix", "combien", "coût", "tarif"]


def detect_intent(message: str, is_merchant: bool) -> Intent:
    text = message.lower()

    if any(kw in text for kw in _PRICE_KEYWORDS):
        return Intent.VOIR_CATALOGUE

    groups = {**_COMMON_KEYWORDS, **(_MERCHANT_KEYWORDS if is_merchant else _CUSTOMER_KEYWORDS)}
    for intent, keywords in groups.items():
        if any(kw in text for kw in keywords):
            return intent

    return Intent.UNKNOWN
