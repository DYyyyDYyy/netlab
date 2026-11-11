"""initial IPAM schema: vrf, vlan, device, subnet (cidr + EXCLUDE), address (inet)

Revision ID: 0001
Revises:
Create Date: 2026-11-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

device_kind = postgresql.ENUM(
    "router", "host", "server", "dns", "external", name="device_kind", create_type=False
)
subnet_role = postgresql.ENUM(
    "lan", "dmz", "transit", "loopback", "wan", name="subnet_role", create_type=False
)


def upgrade() -> None:
    # btree_gist gives GiST an "=" operator for plain integers (vrf_id) in the EXCLUDE constraint
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    device_kind.create(op.get_bind(), checkfirst=True)
    subnet_role.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "vrf",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("rd", sa.String(32)),
    )
    op.create_table(
        "vlan",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("vid", sa.Integer, nullable=False, unique=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.CheckConstraint("vid BETWEEN 1 AND 4094", name="vlan_vid_range"),
    )
    op.create_table(
        "device",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(63), nullable=False, unique=True),
        sa.Column("kind", device_kind, nullable=False),
    )
    op.create_table(
        "subnet",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(63), nullable=False, unique=True),
        sa.Column("vrf_id", sa.Integer, sa.ForeignKey("vrf.id"), nullable=False),
        sa.Column("prefix", postgresql.CIDR, nullable=False),
        sa.Column("role", subnet_role, nullable=False),
        sa.Column("vlan_id", sa.Integer, sa.ForeignKey("vlan.id")),
        sa.Column("gateway", postgresql.INET),
        sa.CheckConstraint("gateway IS NULL OR gateway << prefix", name="subnet_gateway_inside"),
    )
    # two subnets in the same VRF may never overlap: (vrf_id =) AND (prefix &&)
    op.execute(
        "ALTER TABLE subnet ADD CONSTRAINT subnet_no_overlap "
        "EXCLUDE USING gist (vrf_id WITH =, prefix inet_ops WITH &&)"
    )
    op.create_table(
        "address",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "subnet_id",
            sa.Integer,
            sa.ForeignKey("subnet.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ip", postgresql.INET, nullable=False),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("device.id")),
        sa.Column("interface", sa.String(15)),
        sa.Column("dns_name", sa.String(63)),
        sa.Column("description", sa.String(255)),
        sa.UniqueConstraint("subnet_id", "ip", name="address_unique_in_subnet"),
        # a host address, not a prefix: /32 for IPv4, /128 for IPv6
        sa.CheckConstraint(
            "masklen(ip) = CASE family(ip) WHEN 4 THEN 32 ELSE 128 END", name="address_is_host"
        ),
    )
    op.create_index("address_device_idx", "address", ["device_id"])
    op.create_index(
        "address_dns_name_uq",
        "address",
        ["dns_name"],
        unique=True,
        postgresql_where=sa.text("dns_name IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("address")
    op.drop_table("subnet")
    op.drop_table("device")
    op.drop_table("vlan")
    op.drop_table("vrf")
    subnet_role.drop(op.get_bind(), checkfirst=True)
    device_kind.drop(op.get_bind(), checkfirst=True)
