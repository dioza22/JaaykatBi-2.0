import pytest

from app.services.conversation.intents import Intent, detect_intent


@pytest.mark.parametrize(
    ("text", "is_merchant", "expected"),
    [
        ("Bonjour", False, Intent.GREETING),
        ("Dall leen ak jàmm", False, Intent.GREETING),
        ("Je veux commander du riz", False, Intent.COMMANDER_PRODUIT),
        ("Combien coûte le riz ?", False, Intent.VOIR_CATALOGUE),  # price -> redirected to catalogue
        ("Je voudrais voir le catalogue", False, Intent.VOIR_CATALOGUE),
        ("Avez-vous des promotions ?", False, Intent.VOIR_PROMOTIONS),
        ("Je veux annuler ma commande", False, Intent.ANNULER_COMMANDE),
        ("Quel est le statut de ma commande ?", False, Intent.STATUT_COMMANDE),
        ("Je veux parler à quelqu'un", False, Intent.PARLER_A_QUELQUUN),
        ("blablabla xyz", False, Intent.UNKNOWN),
        ("Je veux ajouter un produit", True, Intent.AJOUTER_PRODUIT),
        ("Montre-moi mes ventes", True, Intent.CONSULTER_VENTES),
        ("Je veux lancer une promotion", True, Intent.LANCER_PROMOTION),
        ("Je veux voir mes commandes", False, Intent.MES_COMMANDES_CLIENT),
        ("Je veux définir des frais d'annulation", True, Intent.DEFINIR_FRAIS_ANNULATION),
    ],
)
def test_detect_intent(text, is_merchant, expected):
    assert detect_intent(text, is_merchant) == expected
