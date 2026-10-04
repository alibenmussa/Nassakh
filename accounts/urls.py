"""Login and logout, the sign-up (D106), the organisation's page (D98) and its font files, «الفوترة» (D106,
superusers). Mounted at /accounts/.

`api_urlpatterns` (the book page's «قوالب المؤسسة») are mounted at /api/ in the shared `api` namespace.
"""

from django.urls import path

from . import api, billing_views, views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login, name="login"),
    path("logout/", views.logout, name="logout"),
    path("signup/", views.signup, name="signup"),
    path("signup/sent/", views.signup_sent, name="signup_sent"),
    path("signup/resend/", views.signup_resend, name="signup_resend"),
    path("confirm/<str:token>/", views.confirm_email, name="confirm_email"),
    path("billing/", billing_views.accounts_list, name="billing"),
    path("billing/plans/", billing_views.plans, name="billing_plans"),
    path("billing/plans/<int:plan_id>/", billing_views.plan_edit, name="billing_plan_edit"),
    path("billing/<int:organization_id>/", billing_views.account, name="billing_account"),
    path("billing/<int:organization_id>/grants/", billing_views.grant_add, name="billing_grant_add"),
    path("billing/<int:organization_id>/unlimited/", billing_views.set_unlimited, name="billing_unlimited"),
    path("billing/grants/<int:grant_id>/revoke/", billing_views.grant_revoke, name="billing_grant_revoke"),
    path("organization/", views.organization, name="organization"),
    path("organization/rename/", views.organization_rename, name="organization_rename"),
    path("organization/switch/", views.organization_switch, name="organization_switch"),
    path("organization/fonts/", views.font_upload, name="font_upload"),
    path("organization/fonts/<int:font_id>/", views.font_edit, name="font_edit"),
    path("organization/fonts/<int:font_id>/remove/", views.font_remove, name="font_remove"),
    path("organization/fonts/<int:font_id>/restore/", views.font_restore, name="font_restore"),
    path("organization/fonts/<int:font_id>/delete/", views.font_delete, name="font_delete"),
    path("organization/templates/", views.template_create, name="template_create"),
    path("organization/templates/<int:template_id>/", views.template_edit, name="template_edit"),
    path("organization/templates/<int:template_id>/update/", views.template_update, name="template_update"),
    path("organization/templates/<int:template_id>/delete/", views.template_delete, name="template_delete"),
    path("organization/templates/<int:template_id>/apply/", views.template_apply, name="template_apply"),
    path("fonts/<int:font_id>/<str:filename>", views.font_file, name="font_file"),
]

api_urlpatterns = [
    path("books/<int:book_id>/templates/", api.book_templates, name="book_templates"),
    path(
        "books/<int:book_id>/templates/<int:template_id>/update/",
        api.book_template_update,
        name="book_template_update",
    ),
    path(
        "books/<int:book_id>/templates/<int:template_id>/apply/",
        api.book_template_apply,
        name="book_template_apply",
    ),
]
