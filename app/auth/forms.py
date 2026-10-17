"""WTForms for authentication, invitations and password resets."""

from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, Length, Optional, Regexp

from app.services import crypto_service

USERNAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$"


class LoginForm(FlaskForm):
    """Login form.

    ``identifier`` accepts a username or an email address. The password is not
    length-validated here because a wrong password must produce the same generic
    message as an unknown username.
    """

    identifier = StringField(
        "Username or email",
        validators=[
            DataRequired(message="Enter your username or email."),
            Length(min=3, max=255),
        ],
    )
    password = PasswordField(
        "Password",
        validators=[
            DataRequired(message="Enter your password."),
            Length(max=200),
        ],
    )
    remember = BooleanField("Keep me signed in on this device", default=False)
    submit = SubmitField("Sign in")


class ForgotPasswordForm(FlaskForm):
    email = StringField(
        "Email address",
        validators=[
            DataRequired(message="Enter your email address."),
            Email(message="Enter a valid email address."),
            Length(max=255),
        ],
    )
    submit = SubmitField("Send reset link")


class ResetPasswordForm(FlaskForm):
    password = PasswordField(
        "New password",
        validators=[DataRequired(message="Choose a new password."), Length(max=200)],
    )
    confirm = PasswordField(
        "Confirm new password",
        validators=[DataRequired(message="Repeat the new password."), Length(max=200)],
    )
    submit = SubmitField("Set new password")

    def validate(self, extra_validators=None):  # type: ignore[override]
        if not super().validate(extra_validators):
            return False
        if self.password.data != self.confirm.data:
            self.confirm.errors.append("The two passwords do not match.")
            return False
        try:
            crypto_service.validate_password_strength(self.password.data)
        except crypto_service.PasswordError as exc:
            self.password.errors.append(str(exc))
            return False
        return True


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField(
        "Current password", validators=[DataRequired(message="Enter your current password.")]
    )
    password = PasswordField(
        "New password",
        validators=[DataRequired(message="Choose a new password."), Length(max=200)],
    )
    confirm = PasswordField(
        "Confirm new password",
        validators=[DataRequired(message="Repeat the new password."), Length(max=200)],
    )
    submit = SubmitField("Update password")

    def validate(self, extra_validators=None):  # type: ignore[override]
        if not super().validate(extra_validators):
            return False
        if self.password.data != self.confirm.data:
            self.confirm.errors.append("The two passwords do not match.")
            return False
        try:
            crypto_service.validate_password_strength(self.password.data)
        except crypto_service.PasswordError as exc:
            self.password.errors.append(str(exc))
            return False
        return True


class AcceptInviteForm(FlaskForm):
    username = StringField(
        "Username",
        validators=[
            DataRequired(message="Choose a username."),
            Regexp(USERNAME_PATTERN, message="Use 3-64 letters, digits, dots, dashes or underscores."),
        ],
    )
    full_name = StringField(
        "Full name",
        validators=[DataRequired(message="Enter your full name."), Length(min=2, max=160)],
    )
    password = PasswordField(
        "Password",
        validators=[DataRequired(message="Choose a password."), Length(max=200)],
    )
    confirm = PasswordField(
        "Confirm password",
        validators=[DataRequired(message="Repeat the password."), Length(max=200)],
    )
    timezone = StringField(
        "Time zone",
        validators=[Optional(), Length(max=64)],
        default="Asia/Karachi",
    )
    submit = SubmitField("Create my account")

    def validate(self, extra_validators=None):  # type: ignore[override]
        if not super().validate(extra_validators):
            return False
        if self.password.data != self.confirm.data:
            self.confirm.errors.append("The two passwords do not match.")
            return False
        try:
            crypto_service.validate_password_strength(self.password.data)
        except crypto_service.PasswordError as exc:
            self.password.errors.append(str(exc))
            return False
        return True
