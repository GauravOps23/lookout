"""sensei: conversations, messages, tool-call audit

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def _base():
    return [sa.Column('id', sa.Uuid(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False)]


def upgrade() -> None:
    op.create_table('llm_conversations',
        sa.Column('user_id', sa.Uuid(), nullable=False), sa.Column('title', sa.String(length=128), nullable=False),
        *_base(), sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'), sa.PrimaryKeyConstraint('id'))
    op.create_index(op.f('ix_llm_conversations_user_id'), 'llm_conversations', ['user_id'])
    op.create_table('llm_messages',
        sa.Column('conversation_id', sa.Uuid(), nullable=False), sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('content', sa.Text(), nullable=False), sa.Column('sources', sa.JSON(), nullable=False),
        *_base(), sa.ForeignKeyConstraint(['conversation_id'], ['llm_conversations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'))
    op.create_index(op.f('ix_llm_messages_conversation_id'), 'llm_messages', ['conversation_id'])
    op.create_table('llm_audit',
        sa.Column('user_id', sa.Uuid(), nullable=False), sa.Column('conversation_id', sa.Uuid(), nullable=False),
        sa.Column('tool', sa.String(length=32), nullable=False), sa.Column('args', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False), sa.Column('error', sa.Text(), nullable=False),
        sa.Column('duration_ms', sa.Integer(), nullable=False), *_base(), sa.PrimaryKeyConstraint('id'))
    op.create_index(op.f('ix_llm_audit_user_id'), 'llm_audit', ['user_id'])
    op.create_index(op.f('ix_llm_audit_conversation_id'), 'llm_audit', ['conversation_id'])


def downgrade() -> None:
    op.drop_table('llm_audit')
    op.drop_table('llm_messages')
    op.drop_table('llm_conversations')
