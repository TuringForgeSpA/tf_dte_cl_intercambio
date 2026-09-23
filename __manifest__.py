# -*- coding: utf-8 -*-
{
    'name': 'Facturación electrónica SII Chile - Intercambio',
    'summary': 'Envío del DTE al receptor y recepción de documentos de proveedores.',
    'description': 'Intercambio de documentos tributarios electrónicos con clientes y proveedores. '
                   'Ver README.md para la documentación completa.',
    'version': '18.0.3.0.0',
    'category': 'Accounting/Localizations/EDI',
    'license': 'LGPL-3',
    'author': 'TF',
    'depends': [
        'tf_dte_cl',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/product_data.xml',
        'data/mail_template_data.xml',
        'data/cron.xml',
        'views/dte_received_views.xml',
        'views/account_move_views.xml',
        'views/res_config_settings_views.xml',
        'views/menuitem.xml',
    ],
    'installable': True,
    'application': False,
}
