from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.hazmat.primitives.asymmetric import rsa
from datetime import datetime, timedelta
import os

def create_pfx_if_missing(pfx_path: str, pfx_password: str):
    """
    Automatically generate a RSA4096 self-signed certificate + PKCS#12 (.pfx)
    on first run. If the .pfx exists, do nothing.
    """

    if os.path.exists(pfx_path):
        return  # Nothing to do

    print("🔐 No PFX certificate found — generating new digital signing certificate...")
    print("   This will only happen once.")

    # 1. Generate RSA keypair
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=4096,
    )

    # 2. Build self-signed certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "BAJA SAE Brasil DocGen"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "BAJA SAE Brasil"),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.utcnow() - timedelta(days=1))
        .not_valid_after(datetime.utcnow() + timedelta(days=3650))  # 10 years
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None),
            critical=True
        )
        .sign(private_key, hashes.SHA512())
    )

    # 3. Convert to PKCS#12 (.pfx)
    pfx_bytes = pkcs12.serialize_key_and_certificates(
        name=b"BAJA SAE DocGen",
        key=private_key,
        cert=cert,
        cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(
            pfx_password.encode("utf-8")
        ),
    )

    # 4. Save PFX file
    with open(pfx_path, "wb") as f:
        f.write(pfx_bytes)

    print(f"✅ Digital signing certificate created: {pfx_path}")
    print("   Keep this file backed up. Do NOT share it publicly.")

if __name__ == "__main__":
    password = os.environ.get("PFX_PASSWORD", "")
    if not password.strip():
        raise RuntimeError("PFX_PASSWORD is not set or blank. Set it to a non-empty value first.")
    create_pfx_if_missing('./baja_personalizer.pfx', password)
