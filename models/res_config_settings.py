# -*- coding: utf-8 -*-
"""Ajustes del intercambio.

Ruta real: models/res_config_settings.py
"""
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    tf_dte_cl_exchange_auto_send = fields.Boolean(
        related='company_id.tf_dte_cl_exchange_auto_send', readonly=False,
    )
    tf_dte_cl_exchange_product_id = fields.Many2one(
        related='company_id.tf_dte_cl_exchange_product_id', readonly=False,
    )
    tf_dte_cl_exchange_alert_days = fields.Integer(
        related='company_id.tf_dte_cl_exchange_alert_days', readonly=False,
    )
    tf_dte_cl_exchange_responsible_id = fields.Many2one(
        related='company_id.tf_dte_cl_exchange_responsible_id', readonly=False,
    )
