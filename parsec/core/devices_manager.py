import re
import attr
from pathlib import Path
from structlog import get_logger
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.signing import SigningKey, VerifyKey
from nacl.pwhash import argon2i
from nacl.secret import SecretBox
import nacl.utils

from parsec.schema import UnknownCheckedSchema, fields, validate
from parsec.core.fs.utils import new_access
from parsec.core.backend_connection import backend_send_anonymous_cmd
from parsec.core.local_db import LocalDB
from parsec.utils import to_jsonb64, from_jsonb64
from parsec import pkcs11_encryption_tool


logger = get_logger()


# TODO: SENSITIVE is really slow which is not good for unittests...
# CRYPTO_OPSLIMIT = argon2i.OPSLIMIT_SENSITIVE
# CRYPTO_MEMLIMIT = argon2i.MEMLIMIT_SENSITIVE
CRYPTO_OPSLIMIT = argon2i.OPSLIMIT_INTERACTIVE
CRYPTO_MEMLIMIT = argon2i.MEMLIMIT_INTERACTIVE


class DeviceLoadingError(Exception):
    pass


class DeviceSavingError(Exception):
    pass


class DeviceConfigureError(Exception):
    pass


class DeviceSavingAlreadyExists(DeviceConfigureError):
    pass


class DeviceConfigureBackendError(DeviceConfigureError):
    pass


class DeviceConfigureNoFoundError(DeviceConfigureError):
    pass


class DeviceConfigurationPasswordError(DeviceConfigureError):
    pass


class DeviceConfigureOutOfDate(DeviceConfigureError):
    pass


class DeviceConfigureNoInvitation(DeviceConfigureError):
    pass


# TODO: use parsec.types instead
USER_ID_PATTERN = r"^[0-9a-zA-Z\-_.]+$"
DEVICE_NAME_PATTERN = r"^[0-9a-zA-Z\-_.]+$"
DEVICE_ID_PATTERN = r"^[0-9a-zA-Z\-_.]+@[0-9a-zA-Z\-_.]+$"


def is_valid_user_id(tocheck):
    return bool(re.match(USER_ID_PATTERN, tocheck))


def is_valid_device_name(tocheck):
    return bool(re.match(DEVICE_NAME_PATTERN, tocheck))


def is_valid_device_id(tocheck):
    return bool(re.match(DEVICE_ID_PATTERN, tocheck))


class DeviceConfSchema(UnknownCheckedSchema):
    device_id = fields.String(validate=validate.Regexp(DEVICE_ID_PATTERN), required=True)
    root_verify_key = fields.Base64Bytes(required=True)
    user_privkey = fields.Base64Bytes(required=True)
    device_signkey = fields.Base64Bytes(required=True)
    user_manifest_access = fields.Base64Bytes(required=True)
    local_symkey = fields.Base64Bytes(required=True)
    encryption = fields.String(
        validate=validate.OneOf({"quedalle", "password", "pkcs11"}), required=True
    )
    salt = fields.Base64Bytes()


class UserManifestAccessSchema(UnknownCheckedSchema):
    id = fields.UUID(required=True)
    rts = fields.String(required=True)
    wts = fields.String(required=True)
    key = fields.Base64Bytes(required=True, validate=validate.Length(min=32, max=32))


class BackendGetConfigurationTryRepSchema(UnknownCheckedSchema):
    status = fields.CheckedConstant("ok", required=True)
    device_name = fields.String(required=True)
    configuration_status = fields.String(required=True)
    device_verify_key = fields.Base64Bytes(required=True)
    exchange_cipherkey = fields.Base64Bytes(required=True)
    salt = fields.Base64Bytes(required=True)


device_conf_schema = DeviceConfSchema()
user_manifest_access_schema = UserManifestAccessSchema()
backend_get_configuration_try_rep_schema = BackendGetConfigurationTryRepSchema()


def dumps_user_manifest_access(access):
    user_manifest_access_raw, errors = user_manifest_access_schema.dumps(access)
    assert not errors
    return user_manifest_access_raw.encode("utf8")


def loads_user_manifest_access(data):
    user_manifest_access, errors = user_manifest_access_schema.loads(data.decode("utf8"))
    assert not errors
    return user_manifest_access


def _secret_box_factory(password, salt):
    key = argon2i.kdf(
        SecretBox.KEY_SIZE, password, salt, opslimit=CRYPTO_OPSLIMIT, memlimit=CRYPTO_MEMLIMIT
    )
    return SecretBox(key)


def _generate_salt():
    return nacl.utils.random(argon2i.SALTBYTES)


class Device:
    def __repr__(self):
        return f"<{type(self).__name__}(id={self.id!r}, local_db={self.local_db!r})>"

    def __init__(
        self,
        id,
        root_verify_key,
        user_privkey,
        device_signkey,
        local_symkey,
        user_manifest_access,
        local_db,
    ):
        assert is_valid_device_id(id)
        self.id = id
        self.user_id, self.device_name = id.split("@")
        self.root_verify_key = VerifyKey(root_verify_key)
        self.user_privkey = PrivateKey(user_privkey)
        self.device_signkey = SigningKey(device_signkey)
        self.local_symkey = local_symkey
        self.user_manifest_access = user_manifest_access
        self.local_db = local_db

    @property
    def user_pubkey(self):
        return self.user_privkey.public_key

    @property
    def device_verifykey(self):
        return self.device_signkey.verify_key


class LocalDevicesManager:
    def __init__(self, devices_conf_path):
        self.devices_conf_path = Path(devices_conf_path)

    def list_available_devices(self):
        try:
            candidate_pathes = list(self.devices_conf_path.iterdir())
        except FileNotFoundError:
            return []

        # Sanity check
        devices = []
        for device_path in candidate_pathes:
            _, errors = self._load_device_conf(device_path.name)
            if errors:
                logger.warning("Invalid device config", device_path=device_path, errors=errors)
            else:
                devices.append(device_path.name)
        return devices

    def _load_device_conf(self, device_id):
        errors = {}

        if not is_valid_device_id(device_id):
            errors[device_id] = "Invalid device id"
            return None, errors

        device_key_path = self.devices_conf_path / device_id / "key.json"
        if not device_key_path.is_file():
            errors[device_key_path] = "Missing key file"
            return None, errors

        device_conf, errors = device_conf_schema.loads(device_key_path.read_text())
        if errors:
            return None, errors

        return device_conf, errors

    def register_new_device(
        self,
        device_id,
        root_verify_key,
        user_privkey,
        device_signkey,
        user_manifest_access,
        password=None,
        use_pkcs11=False,
        pkcs11_token_id=0,
        pkcs11_key_id=0,
    ):
        device_conf_path = self.devices_conf_path / device_id
        try:
            device_conf_path.mkdir(parents=True)
        except FileExistsError as exc:
            raise DeviceSavingAlreadyExists(
                f"Device config `{device_conf_path}` already exists"
            ) from exc

        if password and use_pkcs11:
            DeviceSavingError(
                "Password or PKCS #11 required, and password must by empty when using PKCS #11"
            )

        user_manifest_access_raw, _ = user_manifest_access_schema.dumps(user_manifest_access)
        device_conf = {"device_id": device_id, "root_verify_key": root_verify_key}
        local_symkey = nacl.utils.random(nacl.secret.SecretBox.KEY_SIZE)
        if password:
            salt = _generate_salt()
            box = _secret_box_factory(password.encode("utf8"), salt)
            device_conf["salt"] = salt
            device_conf["encryption"] = "password"
            device_conf["device_signkey"] = box.encrypt(device_signkey)
            device_conf["user_privkey"] = box.encrypt(user_privkey)
            device_conf["local_symkey"] = box.encrypt(local_symkey)
            device_conf["user_manifest_access"] = box.encrypt(
                user_manifest_access_raw.encode("utf8")
            )
        else:
            device_conf["encryption"] = "quedalle"
            # Feel dirty just writting this...
            device_conf["device_signkey"] = device_signkey
            device_conf["user_privkey"] = user_privkey
            device_conf["local_symkey"] = local_symkey
            device_conf["user_manifest_access"] = user_manifest_access_raw.encode("utf8")

        if use_pkcs11:
            device_conf["encryption"] = "pkcs11"
            for key in ["device_signkey", "user_privkey", "local_symkey", "user_manifest_access"]:
                try:
                    device_conf[key] = pkcs11_encryption_tool.encrypt_data(
                        pkcs11_token_id, pkcs11_key_id, device_conf[key]
                    )
                except pkcs11_encryption_tool.NoKeysFound:
                    raise pkcs11_encryption_tool.DevicePKCS11Error(
                        "Invalid PKCS #11 token id or key id"
                    )

        device_key_path = device_conf_path / "key.json"
        data, errors = device_conf_schema.dumps(device_conf, indent=True)
        if errors:
            raise DeviceSavingError(
                f"Invalid device config to save for `{self.devices_conf_path}`: {errors}"
            )

        device_key_path.write_text(data)

    def load_device(
        self, device_id: str, password=None, pkcs11_pin=None, pkcs11_token_id=0, pkcs11_key_id=0
    ):
        device_conf_path = self.devices_conf_path / device_id

        if password and pkcs11_pin:
            DeviceLoadingError("Password must by empty when using PKCS #11")

        device_conf, errors = self._load_device_conf(device_id)
        if errors:
            raise DeviceLoadingError(f"Invalid {device_conf_path} device config: {errors}")

        if password:
            if device_conf["encryption"] != "password":
                raise DeviceLoadingError(
                    f"Invalid `{self.devices_conf_path}` device config: password "
                    f"provided but encryption is `{device_conf['encryption']}`"
                )

            box = _secret_box_factory(password.encode("utf8"), device_conf["salt"])
            try:
                user_privkey = box.decrypt(device_conf["user_privkey"])
                device_signkey = box.decrypt(device_conf["device_signkey"])
                local_symkey = box.decrypt(device_conf["local_symkey"])
                user_manifest_access_raw = box.decrypt(device_conf["user_manifest_access"]).decode(
                    "utf8"
                )
                user_manifest_access, errors = user_manifest_access_schema.loads(
                    user_manifest_access_raw
                )
                # TODO: improve data validation
                assert not errors
            except nacl.exceptions.CryptoError as exc:
                raise DeviceLoadingError(
                    f"Invalid `{device_conf_path}` device config: decryption key failure"
                ) from exc

        else:
            if device_conf["encryption"] not in ["quedalle", "pkcs11"]:
                raise DeviceLoadingError(
                    f"Invalid `{device_conf_path}` device config: no password "
                    f"provided but encryption is `{device_conf['encryption']}`"
                )

            if device_conf["encryption"] == "pkcs11":
                for key in [
                    "user_privkey",
                    "device_signkey",
                    "local_symkey",
                    "user_manifest_access",
                ]:
                    try:
                        device_conf[key] = pkcs11_encryption_tool.decrypt_data(
                            pkcs11_pin, pkcs11_token_id, pkcs11_key_id, device_conf[key]
                        )
                    except pkcs11_encryption_tool.NoKeysFound:
                        raise pkcs11_encryption_tool.DevicePKCS11Error(
                            "Invalid PKCS #11 token id or key id"
                        )

            root_verify_key = device_conf["root_verify_key"]
            user_privkey = device_conf["user_privkey"]
            device_signkey = device_conf["device_signkey"]
            local_symkey = device_conf["local_symkey"]
            user_manifest_access, errors = user_manifest_access_schema.loads(
                device_conf["user_manifest_access"]
            )
            assert not errors

        return Device(
            id=device_id,
            root_verify_key=root_verify_key,
            user_privkey=user_privkey,
            device_signkey=device_signkey,
            local_symkey=local_symkey,
            local_db=LocalDB(device_conf_path / "local_storage"),
            user_manifest_access=user_manifest_access,
        )


async def configure_new_device(
    backend_addr,
    device_id,
    configure_device_token,
    password=None,
    use_pkcs11=False,
    pkcs11_token_id=0,
    pkcs11_key_id=0,
):
    """
    Raises:
        BackendNotAvailable
        DeviceConfigureError
    """
    if (password and use_pkcs11) or (not password and not use_pkcs11):
        raise DeviceConfigureError(
            "Password or PKCS #11 required, and password must by empty when using PKCS #11"
        )

    salt = _generate_salt()
    if password:
        box = _secret_box_factory(password.encode("utf8"), salt)
    else:
        box = None
    user_id, device_name = device_id.split("@")
    exchange_cipherkey_privkey = PrivateKey.generate()
    device_signkey = SigningKey.generate()

    if use_pkcs11:
        try:
            exchange_cipherkey_encrypted = pkcs11_encryption_tool.encrypt_data(
                pkcs11_token_id, pkcs11_key_id, exchange_cipherkey_privkey.public_key.encode()
            )
        except pkcs11_encryption_tool.NoKeysFound:
            raise DeviceConfigureError("Invalid PKCS #11 token id or key id")
    else:
        exchange_cipherkey_encrypted = box.encrypt(exchange_cipherkey_privkey.public_key.encode())

    rep = await backend_send_anonymous_cmd(
        backend_addr,
        {
            "cmd": "device_configure",
            "user_id": user_id,
            "device_name": device_name,
            "configure_device_token": configure_device_token,
            "device_verify_key": to_jsonb64(device_signkey.verify_key.encode()),
            "exchange_cipherkey": to_jsonb64(exchange_cipherkey_encrypted),
            "salt": to_jsonb64(salt),
        },
    )

    # TODO: better answer deserialization
    if rep["status"] != "ok":
        raise DeviceConfigureError(rep["reason"])

    ciphered = from_jsonb64(rep["ciphered_user_privkey"])
    box = SealedBox(exchange_cipherkey_privkey)
    try:
        user_privkey_raw = box.decrypt(ciphered)
        user_privkey = PrivateKey(user_privkey_raw)
    except nacl.exceptions.CryptoError as exc:
        raise DeviceConfigureError() from exc

    ciphered = from_jsonb64(rep["ciphered_user_manifest_access"])
    try:
        user_manifest_access_raw = box.decrypt(ciphered)
    except nacl.exceptions.CryptoError as exc:
        raise DeviceConfigureError() from exc
    user_manifest_access, errors = user_manifest_access_schema.loads(user_manifest_access_raw)
    # TODO: improve data validation
    assert not errors

    return user_privkey, device_signkey, user_manifest_access


@attr.s
class ConfigurationTry:
    device_name = attr.ib()
    configuration_status = attr.ib()
    device_verify_key = attr.ib()
    exchange_cipherkey = attr.ib()
    salt = attr.ib()


async def get_device_configuration_try(backend_cmds_sender, config_try_id):
    """
    Raises:
        BackendNotAvailable
        DeviceConfigureNoFoundError
    """
    rep = await backend_cmds_sender.send(
        {"cmd": "device_get_configuration_try", "config_try_id": config_try_id}
    )
    data, errors = backend_get_configuration_try_rep_schema.load(rep)
    if errors:
        if data.get("status") == "not_found":
            raise DeviceConfigureNoFoundError
        raise DeviceConfigureBackendError(f"Bad response from backend: {rep!r} ({errors!r})")

    # TODO: deserialization
    if rep["status"] != "ok":
        raise DeviceConfigureBackendError()
    return ConfigurationTry(
        device_name=rep["device_name"],
        configuration_status=rep["configuration_status"],
        device_verify_key=from_jsonb64(rep["device_verify_key"]),
        exchange_cipherkey=from_jsonb64(rep["exchange_cipherkey"]),
        salt=from_jsonb64(rep["salt"]),
    )


async def accept_device_configuration_try(
    backend_cmds_sender,
    device,
    config_try_id,
    password=None,
    pkcs11_pin=None,
    pkcs11_token_id=0,
    pkcs11_key_id=0,
):
    """
    Raises:
        BackendNotAvailable
        DeviceConfigurationPasswordError
    """
    if (password and pkcs11_pin) or (not password and not pkcs11_pin):
        raise DeviceConfigurationPasswordError("Password must by empty when using PKCS #11")

    config_try = await get_device_configuration_try(backend_cmds_sender, config_try_id)

    if password:
        try:
            box = _secret_box_factory(password.encode("utf8"), config_try.salt)
            exchange_cipherkey_raw = box.decrypt(config_try.exchange_cipherkey)
            box = SealedBox(PublicKey(exchange_cipherkey_raw))
            ciphered_user_privkey = box.encrypt(device.user_privkey.encode())
            user_manifest_access_raw, errors = user_manifest_access_schema.dumps(
                device.user_manifest_access
            )
            assert not errors, errors
            ciphered_user_manifest_access = box.encrypt(user_manifest_access_raw.encode("utf8"))
        except nacl.exceptions.CryptoError as exc:
            raise DeviceConfigurationPasswordError(str(exc)) from exc

    if pkcs11_pin:
        try:
            exchange_cipherkey_raw = pkcs11_encryption_tool.decrypt_data(
                pkcs11_pin, pkcs11_token_id, pkcs11_key_id, config_try.exchange_cipherkey
            )
        except Pkcs11EncryptionError:
            raise DeviceConfigurationPasswordError("Invalid PKCS #11 token id or key id")
        except RuntimeError:
            raise DeviceConfigurationPasswordError("Invalid PKCS #11 PIN")
        try:
            box = SealedBox(PublicKey(exchange_cipherkey_raw))
            ciphered_user_privkey = box.encrypt(device.user_privkey.encode())
            user_manifest_access_raw, errors = user_manifest_access_schema.dumps(
                device.user_manifest_access
            )
            assert not errors, errors
            ciphered_user_manifest_access = box.encrypt(user_manifest_access_raw.encode("utf8"))
        except nacl.exceptions.CryptoError as exc:
            raise DeviceConfigurationPasswordError(str(exc)) from exc

    rep = await backend_cmds_sender.send(
        {
            "cmd": "device_accept_configuration_try",
            "config_try_id": config_try_id,
            "ciphered_user_privkey": to_jsonb64(ciphered_user_privkey),
            "ciphered_user_manifest_access": to_jsonb64(ciphered_user_manifest_access),
        }
    )
    # TODO: deserialization
    if rep["status"] != "ok":
        raise DeviceConfigureBackendError()


async def refuse_device_configuration_try(backend_cmds_sender, config_try_id, reason):
    """
    Raises:
        BackendNotAvailable
        DeviceConfigureBackendError
    """
    rep = await backend_cmds_sender.send(
        {"cmd": "device_refuse_configuration_try", "config_try_id": config_try_id, "reason": reason}
    )
    # TODO: deserialization
    if rep["status"] != "ok":
        raise DeviceConfigureBackendError()


async def invite_user(backend_cmds_sender, user_id):
    """
    Raises:
        BackendNotAvailable
        DeviceConfigureBackendError
    """
    rep = await backend_cmds_sender.send({"cmd": "user_invite", "user_id": user_id})
    # TODO: deserialization
    if rep["status"] != "ok":
        raise DeviceConfigureBackendError()
    return rep["invitation_token"]


async def claim_user(backend_addr, user_id, device_name, invitation_token):
    user_privkey = PrivateKey.generate()
    device_signkey = SigningKey.generate()
    rep = await backend_send_anonymous_cmd(
        backend_addr,
        {
            "cmd": "user_claim",
            "user_id": user_id,
            "device_name": device_name,
            "invitation_token": invitation_token,
            "broadcast_key": to_jsonb64(user_privkey.public_key.encode()),
            "device_verify_key": to_jsonb64(device_signkey.verify_key.encode()),
        },
    )
    # TODO: deserialization
    if rep["status"] != "ok":
        if rep.get("status") == "out_of_date_error":
            raise DeviceConfigureOutOfDate("Claim code is too old.")
        elif rep.get("status") == "not_found_error":
            raise DeviceConfigureNoInvitation("No invitation for this user.")
        elif rep.get("status") == "already_exists_error":
            raise DeviceSavingAlreadyExists("User already exists.")
        raise DeviceConfigureBackendError()

    user_manifest_access = new_access()

    # Note we don't need to upload the user manifest (this will be done
    # lazily)

    return user_privkey, device_signkey, user_manifest_access


async def declare_device(backend_cmds_sender, device_name):
    """
    Raises:
        BackendNotAvailable
        DeviceConfigureBackendError
    """
    rep = await backend_cmds_sender.send({"cmd": "device_declare", "device_name": device_name})
    # TODO: deserialization
    if rep["status"] != "ok":
        raise DeviceConfigureBackendError()
    return rep["configure_device_token"]
