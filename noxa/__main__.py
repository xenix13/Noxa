"""Entry point: python3 -m noxa"""
import argparse
import logging
import os
import signal
import sys
import threading

from .api import Ctx, build_app, janitor
from .web import Server, make_handler
from . import env


def env_bool(name, default=False):
    v = env(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def admin_command(argv):
    """Emergency commands, on the server (console access):
    reset-2fa <username>   turns off two-factor authentication for an account (phone and recovery codes lost)
    list-users             lists the accounts and their two-factor authentication state
    restore <archive>      prepares the restore of a backup (applied at the next start)"""
    from .db import DB
    from . import twofa
    ap = argparse.ArgumentParser(prog="noxa", description="Commandes d'administration de Noxa (en console)")
    ap.add_argument("--data", default=env("DATA", "/var/lib/seance"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reset-2fa", help="désactiver la double authentification d'un compte")
    r.add_argument("username")
    sub.add_parser("list-users", help="lister les comptes")
    rs = sub.add_parser("restore", help="restaurer une sauvegarde (Noxa arrêté, ou redémarré ensuite)")
    rs.add_argument("archive")
    rs.add_argument("--passphrase", help="phrase secrète d'une archive chiffrée (.enc)")
    args = ap.parse_args(argv)
    if args.cmd == "restore":
        from . import backup
        os.makedirs(args.data, mode=0o700, exist_ok=True)
        try:
            archive, decrypted = args.archive, False
            if backup.is_encrypted(archive):
                if not args.passphrase:
                    print("Archive chiffrée : ajoutez --passphrase", file=sys.stderr)
                    return 1
                archive = os.path.join(args.data, "restore-download.tar.gz")
                backup.decrypt_file(args.archive, archive, args.passphrase)
                decrypted = True
            try:
                man = backup.stage(args.data, archive)
            finally:
                if decrypted and os.path.exists(archive):
                    os.remove(archive)
        except backup.BackupError as e:
            print(str(e), file=sys.stderr)
            return 1
        print(f"Sauvegarde de la version {man.get('version')} prête : elle sera mise en place au prochain démarrage "
              "de Noxa (l'état actuel est gardé dans before-restore-…).")
        return 0
    path = os.path.join(args.data, "seance.db")
    if not os.path.isfile(path):
        print(f"Base introuvable : {path} (option --data)", file=sys.stderr)
        return 2
    db = DB(path)
    if args.cmd == "list-users":
        for u in db.all("SELECT username, display_name, is_admin, disabled, totp_enabled FROM users ORDER BY username"):
            flags = [f for f, on in (("administrateur", u["is_admin"]), ("désactivé", u["disabled"]),
                                     ("double authentification", u["totp_enabled"])) if on]
            print(f"{u['username']:<24} {u['display_name']:<24} {', '.join(flags)}")
        return 0
    from .auth import find_user
    u = find_user(db, args.username)
    if not u:
        print(f"Compte introuvable : {args.username} (voir : noxa list-users)", file=sys.stderr)
        return 1
    if not u["totp_enabled"]:
        print(f"La double authentification n'est pas activée pour {u['username']}.")
        return 0
    twofa.disable(db, u["id"])
    logging.getLogger("noxa").warning("Double authentification de %s réinitialisée en console", u["username"])
    print(f"Double authentification désactivée pour {u['username']}. Connexion possible avec le mot de passe seul ; "
          "elle peut être réactivée dans Paramètres → Profil.")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if any(a in ("reset-2fa", "list-users", "restore") for a in argv):
        return admin_command(argv)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(prog="noxa", description="Noxa — serveur multimédia auto-hébergé")
    ap.add_argument("--host", default=env("HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(env("PORT", "8420")))
    ap.add_argument("--data", default=env("DATA", os.path.join(here, "data")))
    ap.add_argument("--static", default=env("STATIC", os.path.join(here, "static")))
    ap.add_argument("--debug", action="store_true", default=env_bool("DEBUG"))
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.makedirs(args.data, mode=0o700, exist_ok=True)
    ctx = Ctx(args.data, args.static,
              cookie_secure=env_bool("COOKIE_SECURE"),
              trust_proxy=env_bool("TRUST_PROXY"))
    app = build_app(ctx)
    handler = make_handler(app, ctx.trust_proxy, hsts=ctx.cookie_secure)
    server = Server((args.host, args.port), handler)
    stop = threading.Event()
    threading.Thread(target=janitor, args=(ctx, stop), daemon=True).start()
    ctx.watcher.start()
    ctx.tasks.start()
    ctx.fed.start()
    ctx.dlna.start(args.port)
    ctx.http_port = args.port
    if ctx.db.scalar("SELECT 1 FROM cold_jobs WHERE state IN ('queued','running') LIMIT 1"):
        ctx.cold.start()  # archiving jobs interrupted when Noxa stopped
    threading.Timer(60, ctx.prefetch_missing_subtitles).start()

    exit_code = [0]

    def shutdown(*_):
        stop.set()
        ctx.fed.stop()
        ctx.dlna.stop()
        ctx.watcher.stop()
        ctx.tasks.stop()
        ctx.subtitles.stop()
        ctx.transcoder.stop_all()
        ctx.live.stop_all()
        threading.Thread(target=server.shutdown, daemon=True).start()
    def restart():
        # non-zero exit code: systemd (Restart=on-failure) and Docker (restart: unless-stopped) start Noxa again
        logging.getLogger("noxa").warning("Redémarrage de Noxa (restauration d'une sauvegarde)")
        exit_code[0] = 75
        shutdown()
    ctx.restart = restart
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    logging.getLogger("noxa").info("Noxa écoute sur http://%s:%d", args.host, args.port)
    server.serve_forever()
    server.server_close()
    return exit_code[0]


if __name__ == "__main__":
    sys.exit(main())
