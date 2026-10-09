"""Persönliche Zugänge zu Diensten: verbinden, trennen, auflisten, Übernahme des Bestands."""

import logging

from app.auth.kontext import NutzerKontext
from app.auth.tresor import (
    Tresor,
    TresorFehler,
    loesche_geheimnis,
    speichere_geheimnis,
    verbundene_dienste,
)
from app.auth.users import finde_erlaubten_nutzer, merker_gesetzt, setze_merker
from app.db.models import STANDARD_LABEL
from app.mail.konten import DIENST as MAIL
from app.mail.konten import (
    Postfach,
    benenne_um,
    ist_adresse,
    label_aus,
    lade_postfaecher,
    setze_signatur,
    speichere_postfach,
    trenne_postfach,
)
from app.mail.verbindung import MailFehler, pruefe_imap, pruefe_smtp
from app.tools.asana_client import DIENST as ASANA
from app.tools.asana_client import erster_admin, pruefe_token
from app.tools.base import ToolFehler, ToolKontext

log = logging.getLogger(__name__)

# Dienst -> Erklärung, was die Person eingeben soll
DIENSTE = {
    ASANA: (
        "deinen persönlichen Asana-Zugriffstoken (Asana → Profilbild → Einstellungen → Apps → "
        "Entwicklerkonsole → „Neues Zugriffstoken“)"
    ),
    MAIL: "die E-Mail-Adresse und danach das Passwort deines Postfachs",
}
MERKER_ASANA = "asana_token_uebernommen"
KEIN_SCHLUESSEL_TEXT = (
    "/verbinden ist deaktiviert: Auf dem Server fehlt SECRETS_MASTER_KEY. Bitte sprich den "
    "Admin an."
)


class Zugaenge:
    def __init__(self, kontext: ToolKontext) -> None:
        self._kontext = kontext
        self._session_fabrik = kontext.session_fabrik

    @property
    def settings(self):
        return self._kontext.settings

    def _tresor(self) -> Tresor:
        try:
            tresor = Tresor.aus_settings(self._kontext.settings)
        except TresorFehler as exc:
            raise ToolFehler(f"/verbinden ist deaktiviert: {exc}") from None
        if not tresor.verfuegbar:
            raise ToolFehler(KEIN_SCHLUESSEL_TEXT)
        return tresor

    def pruefe_verfuegbar(self) -> None:
        """Wirft einen ToolFehler mit klarer Meldung, wenn kein Hauptschlüssel hinterlegt ist."""
        self._tresor()

    async def verbinde(self, nutzer: NutzerKontext, dienst: str, geheimnis: str) -> str:
        """Prüft die Zugangsdaten beim Dienst, speichert sie verschlüsselt und liefert den
        Namen des Kontos. Das Geheimnis verlässt diese Funktion nur verschlüsselt."""
        tresor = self._tresor()
        if dienst != ASANA:
            raise ToolFehler(f"Unbekannter Dienst. Möglich: {', '.join(DIENSTE)}.")
        geheimnis = geheimnis.strip()
        if not geheimnis or any(zeichen.isspace() for zeichen in geheimnis):
            raise ToolFehler("Das sieht nicht nach einem Token aus (leer oder mit Leerzeichen).")
        konto = await pruefe_token(self._kontext, geheimnis)
        await speichere_geheimnis(self._session_fabrik, tresor, nutzer, dienst, geheimnis)
        return konto

    async def trenne(self, nutzer: NutzerKontext, dienst: str, label: str = STANDARD_LABEL) -> bool:
        return await loesche_geheimnis(self._session_fabrik, nutzer, dienst, label)

    # ---------------------------------------------------------------- Mail

    async def postfaecher(self, nutzer: NutzerKontext) -> list[Postfach]:
        """Die eigenen Postfächer. Aufrufer zeigen davon nur Label und Adresse."""
        return await lade_postfaecher(self._session_fabrik, self._tresor(), nutzer)

    async def verbinde_mail(
        self,
        nutzer: NutzerKontext,
        adresse: str,
        passwort: str,
        imap: tuple[str, int] | None = None,
        smtp: tuple[str, int] | None = None,
    ) -> Postfach:
        """Prüft die Anmeldung an IMAP und SMTP (es wird nichts gesendet) und speichert das
        Postfach erst danach verschlüsselt. Das Passwort verlässt diese Funktion nur
        verschlüsselt und steht in keiner Meldung."""
        tresor = self._tresor()
        settings = self._kontext.settings
        adresse = adresse.strip()
        if not ist_adresse(adresse):
            raise ToolFehler("Das sieht nicht nach einer E-Mail-Adresse aus.")
        if not passwort.strip():
            raise ToolFehler("Das Passwort ist leer.")
        eigene = await self.postfaecher(nutzer)
        # Dieselbe Adresse noch einmal verbinden ersetzt den Eintrag (z. B. neues Passwort).
        vorhanden = next((p for p in eigene if p.adresse.lower() == adresse.lower()), None)
        label = vorhanden.label if vorhanden else label_aus(adresse)
        belegt = {p.label for p in eigene if p is not vorhanden}
        basis, zaehler = label, 2
        while label in belegt:
            label = f"{basis[:26]}-{zaehler}"
            zaehler += 1
        imap_host, imap_port = imap or (settings.mail_imap_host, settings.mail_imap_port)
        smtp_host, smtp_port = smtp or (settings.mail_smtp_host, settings.mail_smtp_port)
        postfach = Postfach(
            label=label,
            adresse=adresse,
            passwort=passwort.strip(),
            imap_host=imap_host,
            imap_port=imap_port,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            signatur=vorhanden.signatur if vorhanden else "",
        )
        for name, pruefe in (("Eingang (IMAP)", pruefe_imap), ("Ausgang (SMTP)", pruefe_smtp)):
            try:
                await pruefe(self._kontext, postfach)
            except MailFehler as exc:
                raise ToolFehler(f"{name}: {exc}.") from None
        await speichere_postfach(self._session_fabrik, tresor, nutzer, postfach)
        return postfach

    async def trenne_mail(self, nutzer: NutzerKontext, label: str) -> bool:
        return await trenne_postfach(self._session_fabrik, nutzer, label)

    async def benenne_mail_um(self, nutzer: NutzerKontext, alt: str, neu: str) -> Postfach:
        return await benenne_um(self._session_fabrik, self._tresor(), nutzer, alt, neu)

    async def setze_signatur(self, nutzer: NutzerKontext, label: str, signatur: str) -> None:
        await setze_signatur(self._session_fabrik, self._tresor(), nutzer, label, signatur)

    async def liste(self, nutzer: NutzerKontext) -> list[str]:
        return await verbundene_dienste(self._session_fabrik, nutzer)


async def uebernehme_asana_token(kontext: ToolKontext) -> bool:
    """Ordnet den bisherigen ASANA_TOKEN aus der .env einmalig dem Konto des Admins zu.

    Läuft bei jedem Start und ist idempotent: Ein Merker verhindert, dass der Token nach einem
    späteren /trennen wieder auftaucht. Ohne SECRETS_MASTER_KEY passiert nichts; der Admin
    arbeitet dann weiter mit dem Token aus der .env, bis der Schlüssel gesetzt ist.
    """
    settings = kontext.settings
    token = settings.asana_token.get_secret_value().strip()
    admin_id = erster_admin(settings)
    if not token or admin_id is None:
        return False
    try:
        tresor = Tresor.aus_settings(settings)
    except TresorFehler:
        log.error("SECRETS_MASTER_KEY ist ungültig; der Asana-Token wurde nicht übernommen")
        return False
    if not tresor.verfuegbar or await merker_gesetzt(kontext.session_fabrik, MERKER_ASANA):
        return False
    admin = await finde_erlaubten_nutzer(kontext.session_fabrik, admin_id)
    if admin is None:
        return False
    uebernommen = ASANA not in await verbundene_dienste(kontext.session_fabrik, admin)
    if uebernommen:
        await speichere_geheimnis(kontext.session_fabrik, tresor, admin, ASANA, token)
    await setze_merker(kontext.session_fabrik, MERKER_ASANA)
    log.info("Asana-Token aus der .env dem Admin-Konto zugeordnet: %s", uebernommen)
    return uebernommen
