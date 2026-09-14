from decimal import Decimal, ROUND_HALF_UP
import re
from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import APIException
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from .inventory_services import general_variant_for, merge_into_stock_batch, stock_merge_target
from .models import AICatalogItem, AISettings, AuditLog, Branch, Debt, Expense, MaterialDelivery, StockDelivery, BusinessSettings, CatalogTransfer, CatalogComposition, CatalogHistory, CatalogItem, CatalogMaterialUsage, CatalogRework, CatalogReworkOutput, CatalogReworkSource, CatalogReworkStockInput, Conversation, Customer, FloristAttendance, FloristProfile, FloristPayment, FloristSalaryEntry, FloristVolumeRate, Flower, FlowerVariant, InstagramSettings, InstagramWebhookEvent, IntegrationSettings, Lead, LeadCatalogUsage, LeadPackagingUsage, LeadStatus, LeadStockUsage, Message, Notification, Packaging, PackagingMovement, PagePermission, Reservation, ReservationPayment, SocialPost, FloristDayOff, FloristFaceSample, FloristStockBalance, FloristStockIssue, StockBatch, StockMovement, Supplier, SupplierDebtAdjustment, SupplierPayment, UserProfile
from .permissions import FLORIST_ALLOWED_PAGES


class DetailValidationError(APIException):
    status_code = 400
    default_code = "invalid"


class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserProfile
        fields = ["role", "language", "branch"]


class PagePermissionSerializer(serializers.ModelSerializer):
    label = serializers.CharField(source="get_page_display", read_only=True)

    class Meta:
        model = PagePermission
        fields = ["id", "user", "page", "label", "can_view", "can_control", "created_at", "updated_at"]

    def validate(self, attrs):
        request = self.context.get("request")
        requester_role = getattr(getattr(getattr(request, "user", None), "profile", None), "role", None)
        page = attrs.get("page") or getattr(self.instance, "page", "")
        if requester_role != "developer" and page in PagePermission.DEVELOPER_ONLY_PAGES:
            raise serializers.ValidationError({"page": "Bu permission faqat developer uchun"})
        user = attrs.get("user") or getattr(self.instance, "user", None)
        target_role = getattr(getattr(user, "profile", None), "role", None)
        if target_role != "developer" and page in PagePermission.DEVELOPER_ONLY_PAGES:
            raise serializers.ValidationError({"page": "Bu permission faqat developer userga beriladi"})
        return attrs


class PagePermissionInputSerializer(serializers.Serializer):
    page = serializers.ChoiceField(choices=PagePermission.PAGE_CHOICES)
    can_view = serializers.BooleanField(default=False)
    can_control = serializers.BooleanField(default=False)


def permission_matrix(user) -> list[dict[str, Any]]:
    rows = {row.page: row for row in user.page_permissions.all()} if getattr(user, "id", None) else {}
    data = []
    role = getattr(getattr(user, "profile", None), "role", None)
    for page, label in PagePermission.PAGE_CHOICES:
        if role != "developer" and page in PagePermission.DEVELOPER_ONLY_PAGES:
            continue
        if role in ["florist", "apprentice"] and page not in FLORIST_ALLOWED_PAGES:
            continue
        row = rows.get(page)
        default_access = bool(user.is_superuser and not row)
        can_view = True if role == "developer" else bool(row.can_view if row else default_access)
        can_control = True if role == "developer" else bool(row.can_control if row else default_access)
        data.append({"page": page, "label": label, "can_view": can_view, "can_control": can_control})
    return data


class UserSerializer(serializers.ModelSerializer):
    profile = UserProfileSerializer(read_only=True)
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "email", "is_active", "profile", "permissions"]

    def get_permissions(self, obj) -> list[dict[str, Any]]:
        return permission_matrix(obj)


class UserWriteSerializer(serializers.ModelSerializer):
    role = serializers.ChoiceField(choices=UserProfile.ROLE_CHOICES, write_only=True, required=False)
    language = serializers.ChoiceField(choices=[("uz", "O‘zbek"), ("ru", "Русский")], write_only=True, required=False)
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), write_only=True, required=False, allow_null=True)
    permissions = PagePermissionInputSerializer(many=True, required=False)
    password = serializers.CharField(write_only=True, required=False, min_length=6)
    profile = UserProfileSerializer(read_only=True)
    permission_matrix = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "email", "password", "is_active", "role", "language", "branch", "permissions", "profile", "permission_matrix"]

    def get_permission_matrix(self, obj) -> list[dict[str, Any]]:
        return permission_matrix(obj)

    def validate(self, attrs):
        request = self.context.get("request")
        requester_role = getattr(getattr(getattr(request, "user", None), "profile", None), "role", None)
        permissions = attrs.get("permissions") or []
        if requester_role != "developer" and any(row["page"] in PagePermission.DEVELOPER_ONLY_PAGES for row in permissions):
            raise serializers.ValidationError({"permissions": "AI settings, integrations va audit log permissionlari faqat developer tomonidan beriladi"})
        return attrs

    def save_permissions(self, user, permissions):
        if permissions is None:
            return
        request = self.context.get("request")
        requester_role = getattr(getattr(getattr(request, "user", None), "profile", None), "role", None)
        target_role = getattr(getattr(user, "profile", None), "role", None)
        if requester_role != "developer" and any(row["page"] in PagePermission.DEVELOPER_ONLY_PAGES for row in permissions):
            raise serializers.ValidationError({"permissions": "AI settings, integrations va audit log permissionlari faqat developer tomonidan beriladi"})
        seen = set()
        for row in permissions:
            if target_role != "developer" and row["page"] in PagePermission.DEVELOPER_ONLY_PAGES:
                continue
            seen.add(row["page"])
            PagePermission.objects.update_or_create(user=user, page=row["page"], defaults={"can_view": row["can_view"] or row["can_control"], "can_control": row["can_control"]})
        queryset = PagePermission.objects.filter(user=user).exclude(page__in=seen)
        if target_role == "developer":
            queryset.delete()
        else:
            queryset.exclude(page__in=PagePermission.DEVELOPER_ONLY_PAGES).delete()
            PagePermission.objects.filter(user=user, page__in=PagePermission.DEVELOPER_ONLY_PAGES).delete()

    def create(self, validated_data):
        role = validated_data.pop("role", "operator")
        language = validated_data.pop("language", "uz")
        branch = validated_data.pop("branch", None)
        permissions = validated_data.pop("permissions", None)
        password = validated_data.pop("password", None)
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        UserProfile.objects.create(user=user, role=role, language=language, branch=branch)
        self.save_permissions(user, permissions)
        return user

    def update(self, instance, validated_data):
        role = validated_data.pop("role", None)
        language = validated_data.pop("language", None)
        branch = validated_data.pop("branch", serializers.empty)
        permissions = validated_data.pop("permissions", None)
        password = validated_data.pop("password", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if password:
            instance.set_password(password)
        instance.save()
        profile, _ = UserProfile.objects.get_or_create(user=instance)
        if role is not None:
            profile.role = role
        if language is not None:
            profile.language = language
        if branch is not serializers.empty:
            profile.branch = branch
        profile.save()
        self.save_permissions(instance, permissions)
        return instance


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8)
    new_password_confirm = serializers.CharField(write_only=True)

    def validate_old_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Eski password noto‘g‘ri")
        return value

    def validate(self, attrs):
        if attrs["new_password"] != attrs["new_password_confirm"]:
            raise serializers.ValidationError({"new_password_confirm": "Yangi password tasdiqlash bilan mos emas"})
        return attrs


class FlowerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Flower
        fields = "__all__"
        extra_kwargs = {"slug": {"required": False, "allow_blank": True, "allow_null": True}}

    def validate_slug(self, value):
        return value or None


class FlowerVariantSerializer(serializers.ModelSerializer):
    flower_detail = FlowerSerializer(source="flower", read_only=True)
    class Meta:
        model = FlowerVariant
        fields = "__all__"


class SupplierSerializer(serializers.ModelSerializer):
    batches_count = serializers.IntegerField(read_only=True)
    total_received_stems = serializers.IntegerField(read_only=True)
    material_deliveries_count = serializers.IntegerField(read_only=True)
    material_received_quantity = serializers.IntegerField(read_only=True)
    flower_purchase_total = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    material_purchase_total = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    purchase_total = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    paid_total = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    manual_debt_total = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    debt_total = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    balance_total = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    overpaid_total = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    balance_status = serializers.SerializerMethodField(read_only=True)
    last_payment_at = serializers.DateField(read_only=True)
    material_deliveries = serializers.SerializerMethodField()

    class Meta:
        model = Supplier
        fields = "__all__"

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_material_deliveries(self, obj):
        if not self.context.get("include_material_deliveries"):
            return []
        deliveries = obj.material_deliveries.prefetch_related("movements__packaging").order_by("-received_at", "-id")[:50]
        rows = []
        for delivery in deliveries:
            movements = [row for row in delivery.movements.all() if row.movement_type == "in"]
            rows.append({
                "id": delivery.id,
                "number": delivery.number,
                "received_at": delivery.received_at,
                "note": delivery.note,
                "is_active": delivery.is_active,
                "item_count": len(movements),
                "total_quantity": sum(row.quantity for row in movements),
                "total_cost": sum((Decimal(row.quantity) * Decimal(row.unit_cost or 0) for row in movements), Decimal("0")),
                "items": [
                    {
                        "movement_id": row.id,
                        "packaging": row.packaging_id,
                        "name_uz": row.packaging.name_uz,
                        "packaging_type": row.packaging.packaging_type,
                        "quantity": row.quantity,
                        "unit_cost": row.unit_cost,
                    }
                    for row in movements
                ],
            })
        return rows

    @extend_schema_field(serializers.CharField())
    def get_balance_status(self, obj):
        balance = Decimal(getattr(obj, "balance_total", 0) or 0)
        if balance > 0:
            return "debt"
        if balance < 0:
            return "overpaid"
        return "closed"


class SupplierPaymentSerializer(serializers.ModelSerializer):
    supplier_detail = serializers.SerializerMethodField(read_only=True)
    method_label = serializers.SerializerMethodField(read_only=True)
    created_by_detail = UserSerializer(source="created_by", read_only=True)

    class Meta:
        model = SupplierPayment
        fields = "__all__"
        read_only_fields = ["created_by"]

    @extend_schema_field(serializers.DictField())
    def get_supplier_detail(self, obj):
        return {"id": obj.supplier_id, "name": obj.supplier.name, "phone": obj.supplier.phone}

    @extend_schema_field(serializers.CharField())
    def get_method_label(self, obj):
        return dict(SupplierPayment.METHOD_CHOICES).get(obj.method, obj.method)

    def validate_amount(self, value):
        if value is None or Decimal(value) <= 0:
            raise serializers.ValidationError("To‘lov summasi noldan katta bo‘lishi kerak.")
        return value


class SupplierDebtAdjustmentSerializer(serializers.ModelSerializer):
    supplier_detail = serializers.SerializerMethodField(read_only=True)
    created_by_detail = UserSerializer(source="created_by", read_only=True)

    class Meta:
        model = SupplierDebtAdjustment
        fields = "__all__"
        read_only_fields = ["created_by"]

    @extend_schema_field(serializers.DictField())
    def get_supplier_detail(self, obj):
        return {"id": obj.supplier_id, "name": obj.supplier.name, "phone": obj.supplier.phone}

    def validate_amount(self, value):
        if value is None or Decimal(value) <= 0:
            raise serializers.ValidationError("Qarz summasi noldan katta bo‘lishi kerak.")
        return value


class FloristProfileRateSerializer(serializers.ModelSerializer):
    class Meta:
        model = FloristVolumeRate
        fields = ["id", "arrangement_type", "volume", "default_stems", "florist_fee", "is_active"]
        read_only_fields = ["id"]


class FloristProfileSerializer(serializers.ModelSerializer):
    user_detail = UserSerializer(source="user", read_only=True)
    salary_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    catalog_count = serializers.IntegerField(read_only=True)
    volume_rates = FloristProfileRateSerializer(many=True, required=False)

    class Meta:
        model = FloristProfile
        fields = "__all__"

    def to_internal_value(self, data):
        if hasattr(data, "copy"):
            data = data.copy()
            for field in ["shop_latitude", "shop_longitude"]:
                value = data.get(field)
                if value not in [None, ""]:
                    data[field] = str(Decimal(str(value)).quantize(Decimal("0.0000000001")))
        return super().to_internal_value(data)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        staff_type = attrs.get("staff_type") or getattr(self.instance, "staff_type", "florist")
        if staff_type == "florist":
            attrs["daily_pay"] = 0
        return attrs

    def sync_volume_rates(self, profile, rows):
        if rows is None:
            return
        seen = set()
        for row in rows:
            seen.add((row["arrangement_type"], row["volume"]))
            FloristVolumeRate.objects.update_or_create(
                florist=profile,
                arrangement_type=row["arrangement_type"],
                volume=row["volume"],
                defaults={
                    "default_stems": row.get("default_stems", 0),
                    "florist_fee": row.get("florist_fee", 0),
                    "is_active": row.get("is_active", True),
                },
            )
        query = FloristVolumeRate.objects.filter(florist=profile)
        for rate in query:
            if (rate.arrangement_type, rate.volume) not in seen:
                rate.is_active = False
                rate.save(update_fields=["is_active", "updated_at"])

    def create(self, validated_data):
        volume_rates = validated_data.pop("volume_rates", None)
        profile = super().create(validated_data)
        self.sync_volume_rates(profile, volume_rates)
        return profile

    def update(self, instance, validated_data):
        volume_rates = validated_data.pop("volume_rates", None)
        profile = super().update(instance, validated_data)
        self.sync_volume_rates(profile, volume_rates)
        if profile.staff_type == "apprentice":
            FloristVolumeRate.objects.filter(florist=profile, is_active=True).update(is_active=False)
        return profile


class FloristAttendanceSerializer(serializers.ModelSerializer):
    florist_detail = FloristProfileSerializer(source="florist", read_only=True)

    class Meta:
        model = FloristAttendance
        fields = "__all__"

    def to_internal_value(self, data):
        if hasattr(data, "copy"):
            data = data.copy()
            for field in ["check_in_latitude", "check_in_longitude", "check_out_latitude", "check_out_longitude"]:
                value = data.get(field)
                if value not in [None, ""]:
                    data[field] = str(Decimal(str(value)).quantize(Decimal("0.0000000001")))
        return super().to_internal_value(data)


class FloristStockIssueSerializer(serializers.ModelSerializer):
    florist_name = serializers.SerializerMethodField(read_only=True)
    batch_detail = serializers.SerializerMethodField(read_only=True)
    kind_label = serializers.SerializerMethodField(read_only=True)
    performed_by_detail = UserSerializer(source="performed_by", read_only=True)

    class Meta:
        model = FloristStockIssue
        fields = "__all__"
        read_only_fields = ["performed_by"]

    @extend_schema_field(serializers.CharField())
    def get_florist_name(self, obj):
        return str(obj.florist)

    @extend_schema_field(serializers.CharField())
    def get_kind_label(self, obj):
        return obj.get_kind_display()

    @extend_schema_field(serializers.DictField())
    def get_batch_detail(self, obj):
        batch = obj.batch
        variant = batch.variant
        return {
            "id": batch.id,
            "batch_number": batch.batch_number,
            "flower": variant.flower.name_uz,
            "variant": variant.name_uz,
            "color": variant.color_uz,
            "height_label": batch.height_label,
            "image_url": batch.image_url or variant.image_url or variant.flower.image_url or "",
            "cost_per_stem": str(batch.cost_per_stem),
        }


class FloristStockBalanceSerializer(serializers.ModelSerializer):
    florist_name = serializers.SerializerMethodField(read_only=True)
    batch_detail = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = FloristStockBalance
        fields = "__all__"

    @extend_schema_field(serializers.CharField())
    def get_florist_name(self, obj):
        return str(obj.florist)

    @extend_schema_field(serializers.DictField())
    def get_batch_detail(self, obj):
        batch = obj.batch
        variant = batch.variant
        return {
            "id": batch.id,
            "batch_number": batch.batch_number,
            "flower": variant.flower.name_uz,
            "variant": variant.name_uz,
            "color": variant.color_uz,
            "height_label": batch.height_label,
            "image_url": batch.image_url or variant.image_url or variant.flower.image_url or "",
            "cost_per_stem": str(batch.cost_per_stem),
            "stems_per_bunch": batch.stems_per_bunch,
        }


class FloristStockIssueRequestSerializer(serializers.Serializer):
    florist = serializers.PrimaryKeyRelatedField(queryset=FloristProfile.objects.all())
    batch = serializers.PrimaryKeyRelatedField(queryset=StockBatch.objects.all())
    quantity_stems = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(required=False, allow_blank=True)
    created_at = serializers.DateTimeField(
        required=False,
        help_text="Chiqim qachon bo‘lgani. O‘tib ketgan kun uchun belgilanadi, berilmasa hozirgi vaqt.",
    )


class FloristStockIssueBulkItemSerializer(serializers.Serializer):
    batch = serializers.PrimaryKeyRelatedField(queryset=StockBatch.objects.all())
    quantity_stems = serializers.IntegerField(min_value=1)


class FloristStockIssueBulkRequestSerializer(serializers.Serializer):
    florist = serializers.PrimaryKeyRelatedField(queryset=FloristProfile.objects.all())
    items = FloristStockIssueBulkItemSerializer(many=True)
    reason = serializers.CharField(required=False, allow_blank=True)
    created_at = serializers.DateTimeField(
        required=False,
        help_text="Chiqim qachon bo‘lgani. O‘tib ketgan kun uchun belgilanadi.",
    )

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Kamida bitta gul tanlash kerak")
        return value


class FloristStockReturnRequestSerializer(FloristStockIssueRequestSerializer):
    kind = serializers.ChoiceField(choices=["return", "waste"], required=False, default="return")


class FloristStockIssueEditSerializer(serializers.Serializer):
    """Floristga chiqarilgan gul sonini to'g'rilash."""

    quantity_stems = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(required=False, allow_blank=True)


class FloristCloseSelectedItemSerializer(serializers.Serializer):
    """Yopiladigan bitta chiqim."""

    florist = serializers.PrimaryKeyRelatedField(queryset=FloristProfile.objects.all())
    batch = serializers.PrimaryKeyRelatedField(queryset=StockBatch.objects.all())
    return_stems = serializers.IntegerField(
        required=False, min_value=0, default=0,
        help_text="Shu chiqimdan skladga qaytariladigan gul soni.",
    )


class FloristCloseSelectedSerializer(serializers.Serializer):
    """Tanlangan chiqimlarni birga yopish.

    Bir nechta floristning chiqimini birga tanlash mumkin. Hammasi bitta
    tranzaksiyada yopiladi — bittasida xato chiqsa hech biri yopilmaydi.
    """

    items = FloristCloseSelectedItemSerializer(many=True)
    absorb_remainder = serializers.BooleanField(required=False, default=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Kamida bitta chiqim tanlang")
        seen = set()
        for row in value:
            key = (row["florist"].id, row["batch"].id)
            if key in seen:
                raise serializers.ValidationError(
                    f"{row['florist']} · {row['batch'].batch_number} ikki marta tanlangan"
                )
            seen.add(key)
        return value


class FloristCloseIssueSerializer(serializers.Serializer):
    """Chiqarilgan gul tugadi: ortig'i skladga, qolgani kataloglarga."""

    florist = serializers.PrimaryKeyRelatedField(queryset=FloristProfile.objects.all())
    batch = serializers.PrimaryKeyRelatedField(queryset=StockBatch.objects.all(), required=False, allow_null=True)
    return_stems = serializers.IntegerField(
        required=False, min_value=0, default=0,
        help_text="Ortib qolgan va skladga qaytariladigan gul soni. Qolgani kataloglarga bo‘linadi.",
    )
    close_all = serializers.BooleanField(required=False, default=False)
    absorb_remainder = serializers.BooleanField(required=False, default=True)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if not attrs.get("close_all") and not attrs.get("batch"):
            raise serializers.ValidationError({"batch": "Bitta gulni yopishda partiyani tanlash kerak"})
        if attrs.get("close_all") and attrs.get("return_stems"):
            raise serializers.ValidationError({"return_stems": "Umumiy yopishda return_stems ishlatilmaydi"})
        return attrs


class FloristLeftoverRequestSerializer(serializers.Serializer):
    """Florist standartdan farqli gul ishlatganda hisobni to'g'rilash so'rovi."""

    florist = serializers.PrimaryKeyRelatedField(queryset=FloristProfile.objects.all())
    batch = serializers.PrimaryKeyRelatedField(
        queryset=StockBatch.objects.all(), required=False, allow_null=True,
        help_text="to_catalog da berilmasa hamma qoldiq bo‘linadi. to_florist da majburiy.",
    )
    direction = serializers.ChoiceField(
        choices=[("to_catalog", "Qoldiqni katalogga bo‘lish"), ("to_florist", "Katalogdan floristga qaytarish")],
        required=False, default="to_catalog",
        help_text="to_catalog — florist ko‘proq ishlatgan. to_florist — kamroq ishlatgan.",
    )
    quantity_stems = serializers.IntegerField(
        required=False, min_value=1,
        help_text="Faqat to_florist uchun: katalogdan kamaytirib floristga qaytariladigan gul soni.",
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if attrs.get("direction", "to_catalog") == "to_florist":
            if not attrs.get("batch"):
                raise serializers.ValidationError({"batch": "Katalogdan qaytarishda partiyani tanlash kerak"})
            if not attrs.get("quantity_stems"):
                raise serializers.ValidationError({"quantity_stems": "Qaytariladigan gul sonini kiriting"})
        return attrs


class FloristDayOffSerializer(serializers.ModelSerializer):
    florist_name = serializers.SerializerMethodField(read_only=True)
    kind_label = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = FloristDayOff
        fields = "__all__"
        read_only_fields = ["created_by"]

    @extend_schema_field(serializers.CharField())
    def get_florist_name(self, obj):
        return str(obj.florist)

    @extend_schema_field(serializers.CharField())
    def get_kind_label(self, obj):
        return obj.get_kind_display()


class FloristFaceSampleSerializer(serializers.ModelSerializer):
    florist_name = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = FloristFaceSample
        fields = ["id", "florist", "florist_name", "image_url", "is_active", "created_at", "updated_at"]
        read_only_fields = ["created_by"]

    @extend_schema_field(serializers.CharField())
    def get_florist_name(self, obj):
        return str(obj.florist)


class FloristSalaryEntrySerializer(serializers.ModelSerializer):
    florist_detail = FloristProfileSerializer(source="florist", read_only=True)
    catalog_item_detail = serializers.SerializerMethodField()
    reason = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = FloristSalaryEntry
        fields = "__all__"
        read_only_fields = ["created_by"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance and self.instance.source == "daily" and "amount" in attrs and attrs["amount"] != self.instance.amount:
            reason = attrs.get("reason") or attrs.get("note")
            if not reason:
                raise serializers.ValidationError({"reason": "Shogird kunlik ish haqini o‘zgartirish sababi kerak"})
        source = attrs.get("source", getattr(self.instance, "source", ""))
        catalog_item = attrs.get("catalog_item", getattr(self.instance, "catalog_item", None))
        if source in FloristSalaryEntry.PRODUCTION_SOURCES and not catalog_item:
            raise serializers.ValidationError({"catalog_item": "Katalog ish haqi uchun katalog tanlanishi kerak"})
        return attrs

    def normalize_salary_amount(self, validated_data, instance=None):
        source = validated_data.get("source", getattr(instance, "source", ""))
        quantity = validated_data.get("quantity", getattr(instance, "quantity", 0) if instance else 0)
        unit_amount = validated_data.get("unit_amount", getattr(instance, "unit_amount", Decimal("0")) if instance else Decimal("0"))
        catalog_item = validated_data.get("catalog_item", getattr(instance, "catalog_item", None) if instance else None)
        if catalog_item:
            validated_data.setdefault("catalog_name", catalog_item.name_uz or "")
            validated_data.setdefault("catalog_kind", catalog_item.catalog_kind or "")
            validated_data.setdefault("arrangement_type", catalog_item.arrangement_type or "")
            validated_data.setdefault("volume", catalog_item.volume or "")
        if "amount" not in validated_data and quantity and unit_amount:
            validated_data["amount"] = Decimal(str(unit_amount)) * int(quantity)
        elif "amount" in validated_data and source in FloristSalaryEntry.PRODUCTION_SOURCES and not catalog_item and not quantity:
            validated_data["quantity"] = 1
            validated_data["unit_amount"] = validated_data["amount"]
        elif source in FloristSalaryEntry.PRODUCTION_SOURCES and not catalog_item and quantity and not unit_amount:
            validated_data["unit_amount"] = Decimal(str(validated_data.get("amount", 0))) / Decimal(int(quantity))
        return validated_data

    def create(self, validated_data):
        return super().create(self.normalize_salary_amount(validated_data))

    def update(self, instance, validated_data):
        reason = validated_data.pop("reason", "")
        if reason:
            note = validated_data.get("note", instance.note or "")
            validated_data["note"] = (note + "\n" if note else "") + f"O‘zgartirish sababi: {reason}"
        return super().update(instance, self.normalize_salary_amount(validated_data, instance))

    @extend_schema_field(serializers.DictField())
    def get_catalog_item_detail(self, obj):
        if not obj.catalog_item_id:
            return None
        return {"id": obj.catalog_item_id, "name_uz": obj.catalog_item.name_uz, "catalog_kind": obj.catalog_item.catalog_kind, "arrangement_type": obj.catalog_item.arrangement_type}


class FloristPaymentSerializer(serializers.ModelSerializer):
    florist_detail = FloristProfileSerializer(source="florist", read_only=True)
    method_label = serializers.SerializerMethodField(read_only=True)
    created_by_detail = UserSerializer(source="created_by", read_only=True)

    class Meta:
        model = FloristPayment
        fields = "__all__"
        read_only_fields = ["created_by"]

    @extend_schema_field(serializers.CharField())
    def get_method_label(self, obj):
        return dict(FloristPayment.METHOD_CHOICES).get(obj.method, obj.method)

    def validate_amount(self, value):
        if value is None or Decimal(value) <= 0:
            raise serializers.ValidationError("To‘lov summasi noldan katta bo‘lishi kerak.")
        return value


class FloristDecorationSalarySerializer(serializers.Serializer):
    """Floristga qo'lda oformleniya haqi yozish."""

    count = serializers.IntegerField(min_value=1, help_text="Nechta oformleniya qilingani.")
    unit_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=Decimal("0.01"), help_text="Bittasining narxi. Berilmasa florist profilidagi oformleniya narxi olinadi.")
    work_date = serializers.DateField(required=False, help_text="Berilmasa bugungi sana.")
    note = serializers.CharField(required=False, allow_blank=True)


class FloristVolumeRateSerializer(serializers.ModelSerializer):
    florist_name = serializers.SerializerMethodField()

    class Meta:
        model = FloristVolumeRate
        fields = "__all__"

    def validate_florist(self, value):
        if value is None:
            raise serializers.ValidationError("Tarif aniq floristga biriktirilishi kerak. Umumiy tarif ishlatilmaydi.")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        florist = attrs.get("florist", getattr(self.instance, "florist", None))
        if florist is None:
            raise serializers.ValidationError({"florist": "Tarif aniq floristga biriktirilishi kerak. Umumiy tarif ishlatilmaydi."})
        return attrs

    @extend_schema_field(serializers.CharField())
    def get_florist_name(self, obj):
        if not obj.florist_id:
            return ""
        return str(obj.florist)


PRICE_ROUND_STEP = Decimal("100")


def batch_unit_cost(batch):
    if Decimal(batch.cost_per_bunch or 0) > 0 and int(batch.stems_per_bunch or 0) > 0:
        return Decimal(batch.cost_per_bunch) / Decimal(batch.stems_per_bunch)
    return Decimal(batch.cost_per_stem_exact or 0) or Decimal(batch.cost_per_stem or 0)


def batch_unit_sale(batch):
    if Decimal(batch.sale_price_per_bunch or 0) > 0 and int(batch.stems_per_bunch or 0) > 0:
        return Decimal(batch.sale_price_per_bunch) / Decimal(batch.stems_per_bunch)
    return Decimal(batch.sale_price_per_stem_exact or 0) or Decimal(batch.sale_price_per_stem or 0)


def round_stem_price(value):
    """Dona narxi endi yaxlitlanmaydi — aynan hisoblangan qiymat qoladi.

    Avval eng yaqin 100 ga yaxlitlanardi (50 000 / 15 = 3 333,33 dan 3 300).
    Ko'rinishi chiroyli bo'lgani bilan yig'indi qog'ozdagi summaga to'g'ri
    kelmay qolardi, shuning uchun yaxlitlash olib tashlandi.
    """
    amount = Decimal(str(value))
    if amount <= 0:
        return Decimal("0.00")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class StockDeliverySerializer(serializers.ModelSerializer):
    """Partiya — avval ochiladi, keyin ichiga gullar qo'shiladi."""

    supplier_detail = SupplierSerializer(source="supplier", read_only=True)
    created_by_detail = UserSerializer(source="created_by", read_only=True)
    batch_count = serializers.SerializerMethodField()
    total_stems = serializers.SerializerMethodField()
    remaining_stems = serializers.SerializerMethodField()
    total_cost = serializers.SerializerMethodField()
    total_cost_exact = serializers.SerializerMethodField()
    rounding_diff = serializers.SerializerMethodField()

    class Meta:
        model = StockDelivery
        fields = "__all__"
        read_only_fields = ["created_by"]

    @extend_schema_field(serializers.IntegerField())
    def get_batch_count(self, obj):
        return obj.batches.count()

    @extend_schema_field(serializers.IntegerField())
    def get_total_stems(self, obj):
        return sum(row.received_stems for row in obj.batches.all())

    @extend_schema_field(serializers.IntegerField())
    def get_remaining_stems(self, obj):
        return sum(row.remaining_stems for row in obj.batches.all())

    @extend_schema_field(serializers.DecimalField(max_digits=14, decimal_places=2))
    def get_total_cost(self, obj):
        total = Decimal("0")
        for row in obj.batches.all():
            total += Decimal(row.received_stems) * batch_unit_cost(row)
        return total.quantize(Decimal("0.01"))

    @extend_schema_field(serializers.DecimalField(max_digits=14, decimal_places=2))
    def get_total_cost_exact(self, obj):
        """Yaxlitlanmagan aniq hisob bo'yicha."""
        total = Decimal("0")
        for row in obj.batches.all():
            total += Decimal(row.received_stems) * batch_unit_cost(row)
        return total.quantize(Decimal("0.01"))

    @extend_schema_field(serializers.DecimalField(max_digits=14, decimal_places=2))
    def get_rounding_diff(self, obj):
        rounded_total = sum((Decimal(row.received_stems) * Decimal(row.cost_per_stem or 0) for row in obj.batches.all()), Decimal("0"))
        return (rounded_total - Decimal(self.get_total_cost_exact(obj))).quantize(Decimal("0.01"))


class StockBatchSerializer(serializers.ModelSerializer):
    """Partiya ichidagi gul qatori.

    Kirimda nav so'ralmaydi — `flower` yuboriladi. Shu partiyada guli, bo'yi
    va tannarxi bir xil qator allaqachon bo'lsa yangi qator ochilmaydi,
    soni o'sha qatorga qo'shiladi va javobda `merged` true bo'ladi.
    """

    flower = serializers.PrimaryKeyRelatedField(queryset=Flower.objects.all(), required=False, write_only=True, help_text="Gul. Kirimda shu yuboriladi, nav so'ralmaydi.")
    flower_detail = serializers.SerializerMethodField()
    flower_name = serializers.CharField(read_only=True)
    title = serializers.CharField(read_only=True)
    variant_detail = FlowerVariantSerializer(source="variant", read_only=True)
    supplier_detail = SupplierSerializer(source="supplier", read_only=True)
    delivery_detail = serializers.SerializerMethodField()
    rounding = serializers.SerializerMethodField()
    remaining_bunches = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    remaining_bunches_label = serializers.SerializerMethodField()
    stock_value = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    height_label = serializers.CharField(read_only=True)
    received_bunches = serializers.DecimalField(max_digits=10, decimal_places=2, write_only=True, required=False)
    merged = serializers.SerializerMethodField()
    merged_stems = serializers.SerializerMethodField()

    class Meta:
        model = StockBatch
        fields = "__all__"
        extra_kwargs = {
            "variant": {"required": False},
            "received_stems": {"required": False},
            "remaining_stems": {"required": False},
            "batch_number": {"required": False},
            "cost_per_stem": {"required": False},
            "cost_per_stem_exact": {"required": False},
            "sale_price_per_stem_exact": {"required": False},
            "sale_price_per_stem": {"required": False},
            "sale_price_per_bunch": {"required": False},
        }

    @extend_schema_field(serializers.DictField())
    def get_flower_detail(self, obj):
        return FlowerSerializer(obj.variant.flower).data

    @extend_schema_field(serializers.BooleanField())
    def get_merged(self, obj):
        """Kirim mavjud qatorga qo'shildimi yoki yangi qator ochildimi."""
        return bool(getattr(obj, "_merged", False))

    @extend_schema_field(serializers.IntegerField())
    def get_merged_stems(self, obj):
        """Shu kirimda qo'shilgan dona soni. Yangi qatorda kelgan songa teng."""
        return int(getattr(obj, "_merged_stems", obj.received_stems or 0))

    @extend_schema_field(serializers.CharField())
    def get_remaining_bunches_label(self, obj):
        return f"{obj.remaining_bunches} pochka"

    @extend_schema_field(serializers.DictField())
    def get_delivery_detail(self, obj):
        if not obj.delivery_id:
            return None
        delivery = obj.delivery
        return {
            "id": delivery.id,
            "number": delivery.number,
            "received_at": delivery.received_at,
            "supplier": delivery.supplier.name if delivery.supplier_id else "",
            "note": delivery.note,
        }

    @extend_schema_field(serializers.DictField())
    def get_rounding(self, obj):
        """Yaxlitlangan va yaxlitlanmagan narx yonma-yon, farqi bilan."""
        stems = Decimal(obj.received_stems or 0)
        rows = {}
        for key, rounded, exact in [
            ("cost", Decimal(obj.cost_per_stem or 0), Decimal(obj.cost_per_stem_exact or 0)),
            ("sale", Decimal(obj.sale_price_per_stem or 0), Decimal(obj.sale_price_per_stem_exact or 0)),
        ]:
            exact = exact or rounded
            rows[key] = {
                "per_stem_exact": exact.quantize(Decimal("0.0001")),
                "per_stem_rounded": rounded.quantize(Decimal("0.01")),
                "per_stem_diff": (rounded - exact).quantize(Decimal("0.0001")),
                "total_exact": (exact * stems).quantize(Decimal("0.01")),
                "total_rounded": (rounded * stems).quantize(Decimal("0.01")),
                "total_diff": ((rounded - exact) * stems).quantize(Decimal("0.01")),
                "is_rounded": rounded != exact,
            }
        return rows

    def _is_free(self, data):
        value = data.get("is_free") if "is_free" in data else getattr(self.instance, "is_free", False)
        return value in [True, "true", "True", 1, "1"]

    def _fill_prices(self, data):
        """Pochka narxidan dona narxini, dona narxidan pochka narxini to'ldiradi.

        Yaxlitlangan narx bilan birga yaxlitlanmagan aniq hisob ham saqlanadi —
        partiya detalida ikkalasini yonma-yon ko'rsatish uchun.
        """
        if self._is_free(data):
            # tekin gul: tannarx yozilmaydi, sotuv narxi odatdagidek hisoblanadi
            data["cost_per_bunch"] = "0"
            data["cost_per_stem"] = "0"
            data["cost_per_stem_exact"] = "0"
        stems = data.get("stems_per_bunch") or getattr(self.instance, "stems_per_bunch", None)
        try:
            stems = int(stems)
        except (TypeError, ValueError):
            return data
        if stems < 1:
            return data
        pairs = [
            ("cost_per_bunch", "cost_per_stem", "cost_per_stem_exact"),
            ("sale_price_per_bunch", "sale_price_per_stem", "sale_price_per_stem_exact"),
        ]
        for bunch_key, stem_key, exact_key in pairs:
            bunch_value = data.get(bunch_key)
            stem_value = data.get(stem_key)
            if bunch_value not in [None, ""] and stem_value in [None, ""]:
                exact = (Decimal(str(bunch_value)) / Decimal(stems)).quantize(Decimal("0.0001"))
                data[exact_key] = str(exact)
                data[stem_key] = str(round_stem_price(exact))
            elif stem_value not in [None, ""]:
                # dona narxi qo'lda kiritilgan — aniq hisob ham o'shaning o'zi
                data[exact_key] = str(Decimal(str(stem_value)).quantize(Decimal("0.0001")))
                if bunch_value in [None, ""]:
                    data[bunch_key] = str((Decimal(str(stem_value)) * Decimal(stems)).quantize(Decimal("0.01")))
        return data

    def to_internal_value(self, data):
        data = data.copy()
        if getattr(self, "partial", False):
            for key, value in list(data.items()):
                if value == "":
                    data.pop(key)
        if not data.get("height_cm") and data.get("height_from_cm"):
            data["height_cm"] = data["height_from_cm"]
        delivery = StockDelivery.objects.filter(pk=data.get("delivery")).first() if data.get("delivery") else None
        if delivery:
            # partiya raqami, sanasi va postavshigi partiyadan olinadi, qayta so'ralmaydi
            data["batch_number"] = delivery.number
            data.setdefault("received_at", delivery.received_at)
            if delivery.supplier_id and not data.get("supplier"):
                data["supplier"] = delivery.supplier_id
        data = self._fill_prices(data)
        if not data.get("received_stems") and data.get("received_bunches") and data.get("stems_per_bunch"):
            # int() pastga kesardi: 56.7 pochka x 15 dona = 850.5 dan 850 qolib,
            # yarim dona puli partiya summasidan tushib qolardi.
            stems = Decimal(str(data["received_bunches"])) * Decimal(str(data["stems_per_bunch"]))
            data["received_stems"] = int(stems.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        # Qoldiqni kelgan songa tenglash faqat yangi partiyada to'g'ri.
        # Tahrirda ishlatilgan gul unutilib qolmasligi uchun qoldiq
        # viewset da farq bo'yicha siljitiladi.
        if data.get("received_stems") and not data.get("remaining_stems") and not self.instance:
            data["remaining_stems"] = data["received_stems"]
        return super().to_internal_value(data)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Kirimda gul tanlanadi, nav emas. Nav yuborilsa eski usul deb qabul
        # qilinadi — eski partiyalarni tahrirlash uchun kerak.
        flower = attrs.pop("flower", None)
        if flower is not None:
            attrs["variant"] = general_variant_for(flower)
        if not self.instance and not attrs.get("variant"):
            raise serializers.ValidationError({"flower": "Gulni tanlang"})
        if not self.instance:
            # narxni pochkada ham, donada ham berish mumkin, lekin biri bo'lishi shart.
            # Tekin gulda tannarx umuman yozilmaydi.
            if not attrs.get("is_free") and attrs.get("cost_per_stem") is None and not attrs.get("cost_per_bunch"):
                raise serializers.ValidationError({"cost_per_bunch": "Pochka tannarxini yoki dona tannarxini kiriting"})
            if attrs.get("sale_price_per_stem") is None and attrs.get("sale_price_per_bunch") is None:
                raise serializers.ValidationError({"sale_price_per_bunch": "Pochka sotuv narxini yoki dona sotuv narxini kiriting"})
        height_cm = attrs.get("height_cm") or getattr(self.instance, "height_cm", None)
        height_from = attrs.get("height_from_cm")
        height_to = attrs.get("height_to_cm")
        if height_from and height_to and height_from > height_to:
            raise serializers.ValidationError({"height_to_cm": "Bo‘y oralig‘i noto‘g‘ri: height_to_cm height_from_cm dan katta yoki teng bo‘lishi kerak."})
        if height_from and not height_to:
            attrs["height_to_cm"] = height_from
        if height_to and not height_from:
            attrs["height_from_cm"] = height_to
        if not height_from and not height_to and height_cm:
            attrs.setdefault("height_from_cm", height_cm)
            attrs.setdefault("height_to_cm", height_cm)
        return attrs

    def create(self, validated_data):
        validated_data.pop("received_bunches", None)
        # Har bir gul partiya ichida turadi. Partiya tanlanmagan bo'lsa
        # raqam bo'yicha topiladi yoki o'sha zahoti ochiladi.
        if not validated_data.get("delivery"):
            number = (validated_data.get("batch_number") or "").strip()
            if not number:
                raise serializers.ValidationError({"delivery": "Partiyani tanlang yoki partiya raqamini kiriting"})
            received_at = validated_data.get("received_at") or timezone.localdate()
            delivery, _ = StockDelivery.objects.get_or_create(
                number=number,
                received_at=received_at,
                defaults={"supplier": validated_data.get("supplier"), "is_active": True},
            )
            validated_data["delivery"] = delivery
        validated_data["batch_number"] = validated_data["delivery"].number
        with transaction.atomic():
            target = stock_merge_target(validated_data, lock=True)
            if not target:
                batch = super().create(validated_data)
                batch._merged = False
                batch._merged_stems = batch.received_stems
                return batch
            if not int(validated_data.get("received_stems") or 0):
                raise serializers.ValidationError({"received_stems": "Kelgan sonni kiriting"})
            added = merge_into_stock_batch(target, validated_data)
        target._merged = True
        target._merged_stems = added
        return target

    def update(self, instance, validated_data):
        validated_data.pop("received_bunches", None)
        delivery = validated_data.get("delivery") or instance.delivery
        if delivery:
            validated_data["batch_number"] = delivery.number
        return super().update(instance, validated_data)


class StockMovementSerializer(serializers.ModelSerializer):
    batch_detail = StockBatchSerializer(source="batch", read_only=True)
    performed_by_detail = UserSerializer(source="performed_by", read_only=True)
    cost_value = serializers.SerializerMethodField(read_only=True)
    sale_value = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = StockMovement
        fields = "__all__"
        read_only_fields = ["performed_by"]

    @extend_schema_field(serializers.DecimalField(max_digits=16, decimal_places=2))
    def get_cost_value(self, obj):
        stems = abs(int(obj.quantity_stems or 0))
        return str((Decimal(stems) * batch_unit_cost(obj.batch)).quantize(Decimal("0.01")))

    @extend_schema_field(serializers.DecimalField(max_digits=16, decimal_places=2))
    def get_sale_value(self, obj):
        if Decimal(obj.sale_amount or 0) > 0:
            return str(Decimal(obj.sale_amount or 0).quantize(Decimal("0.01")))
        stems = abs(int(obj.quantity_stems or 0))
        return str((Decimal(stems) * batch_unit_sale(obj.batch)).quantize(Decimal("0.01")))


class MaterialDeliverySerializer(serializers.ModelSerializer):
    """Material partiyasi — avval ochiladi, keyin ichiga materiallar kiritiladi."""

    supplier_detail = SupplierSerializer(source="supplier", read_only=True)
    created_by_detail = UserSerializer(source="created_by", read_only=True)
    item_count = serializers.SerializerMethodField()
    total_quantity = serializers.SerializerMethodField()
    total_cost = serializers.SerializerMethodField()

    class Meta:
        model = MaterialDelivery
        fields = "__all__"
        read_only_fields = ["created_by"]

    @extend_schema_field(serializers.IntegerField())
    def get_item_count(self, obj):
        return obj.movements.filter(movement_type="in").count()

    @extend_schema_field(serializers.IntegerField())
    def get_total_quantity(self, obj):
        return sum(row.quantity for row in obj.movements.filter(movement_type="in"))

    @extend_schema_field(serializers.DecimalField(max_digits=14, decimal_places=2))
    def get_total_cost(self, obj):
        return sum(
            (Decimal(row.quantity) * Decimal(row.unit_cost or 0) for row in obj.movements.filter(movement_type="in")),
            Decimal("0"),
        )


class MaterialReceiveSerializer(serializers.Serializer):
    """Partiyaga material kiritish so'rovi.

    Pochkada keladigan materialda `bunches` va `cost_per_bunch` yuborish yetarli —
    dona soni bilan dona tannarxi o'zi hisoblanadi.
    """

    packaging = serializers.PrimaryKeyRelatedField(queryset=Packaging.objects.all())
    quantity = serializers.IntegerField(min_value=1, required=False)
    bunches = serializers.IntegerField(min_value=1, required=False)
    cost_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, min_value=Decimal("0"),
        help_text="Dona tannarxi. Berilmasa materialning hozirgi tannarxi qoladi.",
    )
    cost_per_bunch = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, min_value=Decimal("0"),
        help_text="Pochka narxi. Dona tannarxi shundan hisoblanadi.",
    )
    reason = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        material = attrs["packaging"]
        per_bunch = int(material.units_per_bunch or 20)
        if attrs.get("bunches"):
            attrs["quantity"] = int(attrs["bunches"]) * per_bunch
        if not attrs.get("quantity"):
            raise serializers.ValidationError({"quantity": "Dona sonini yoki pochka sonini kiriting"})
        if attrs.get("cost_per_bunch") is not None and per_bunch > 0:
            attrs["cost_price"] = (Decimal(str(attrs["cost_per_bunch"])) / Decimal(per_bunch)).quantize(Decimal("0.01"))
        return attrs


class PackagingSerializer(serializers.ModelSerializer):
    packaging_type = serializers.ChoiceField(choices=Packaging.TYPE_CHOICES, required=False, default="other")
    image = serializers.FileField(write_only=True, required=False)
    quantity_label = serializers.CharField(read_only=True)
    packaging_type_label = serializers.CharField(source="get_packaging_type_display", read_only=True)
    unit_label = serializers.CharField(source="get_unit_display", read_only=True)
    basket_material_label = serializers.CharField(source="get_basket_material_display", read_only=True)
    last_delivery = serializers.SerializerMethodField()
    # Material qo'shilayotganda darrov yukka bog'lash uchun
    delivery = serializers.PrimaryKeyRelatedField(
        queryset=MaterialDelivery.objects.all(), write_only=True, required=False, allow_null=True,
        help_text="Berilsa material shu yukka kirim qilinadi.",
    )
    bunches = serializers.IntegerField(
        write_only=True, required=False, min_value=1,
        help_text="Pochkada keladigan material uchun: nechta pochka. Dona soni o‘zi hisoblanadi.",
    )
    cost_per_bunch = serializers.DecimalField(
        max_digits=12, decimal_places=2, write_only=True, required=False, min_value=Decimal("0"),
        help_text="Pochka narxi. Dona tannarxi shundan hisoblanadi.",
    )

    class Meta:
        model = Packaging
        fields = "__all__"

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if not self.instance:
            attrs["packaging_type"] = attrs.get("packaging_type") or "other"
            attrs["unit"] = attrs.get("unit") or "piece"
            attrs["cost_price"] = attrs.get("cost_price") or Decimal("0")
            attrs["sale_price"] = attrs.get("sale_price") or Decimal("0")
        kind = attrs.get("packaging_type") or getattr(self.instance, "packaging_type", None)
        size = (attrs.get("size") if "size" in attrs else getattr(self.instance, "size", "")) or ""
        if kind == "basket":
            allowed = [key for key, _ in Packaging.SIZE_CHOICES]
            if size and size.lower() not in allowed:
                raise serializers.ValidationError({"size": f"Savat razmeri: {', '.join(allowed)}"})
            if size:
                attrs["size"] = size.lower()
        if attrs.get("bunches") and not attrs.get("units_per_bunch") and not getattr(self.instance, "units_per_bunch", None):
            attrs["units_per_bunch"] = 20
        return attrs

    def _receive_extras(self, validated_data):
        """Yuk, pochka soni va pochka narxini ajratib oladi."""
        return (
            validated_data.pop("delivery", None),
            validated_data.pop("bunches", None),
            validated_data.pop("cost_per_bunch", None),
        )

    def _apply_bunch_pricing(self, validated_data, bunches, cost_per_bunch):
        """Pochka soni va narxidan dona soni bilan dona tannarxini hisoblaydi."""
        per_bunch = int(validated_data.get("units_per_bunch") or getattr(self.instance, "units_per_bunch", 20) or 20)
        quantity = None
        if bunches:
            quantity = int(bunches) * per_bunch
        if cost_per_bunch is not None and per_bunch > 0:
            validated_data["cost_price"] = (Decimal(str(cost_per_bunch)) / Decimal(per_bunch)).quantize(Decimal("0.01"))
        return quantity

    @extend_schema_field(serializers.DictField())
    def get_last_delivery(self, obj):
        """Oxirgi marta qaysi partiyadan, qaysi postavshikdan kelgani."""
        row = obj.movements.filter(movement_type="in", delivery__isnull=False).select_related("delivery__supplier").order_by("-created_at", "-id").first()
        if not row:
            return None
        return {
            "id": row.delivery_id,
            "number": row.delivery.number,
            "received_at": row.delivery.received_at,
            "supplier": row.delivery.supplier.name if row.delivery.supplier_id else "",
            "supplier_id": row.delivery.supplier_id,
            "quantity": row.quantity,
            "unit_cost": row.unit_cost,
        }

    def save_image(self, validated_data):
        image = validated_data.pop("image", None)
        if image:
            path = default_storage.save(f"materials/{image.name}", image)
            validated_data["image_url"] = default_storage.url(path)
        return validated_data

    def create(self, validated_data):
        from .inventory_services import receive_material_into_delivery

        validated_data = self.save_image(validated_data)
        delivery, bunches, cost_per_bunch = self._receive_extras(validated_data)
        quantity = self._apply_bunch_pricing(validated_data, bunches, cost_per_bunch)
        if quantity is None:
            quantity = int(validated_data.get("quantity") or 0)
        user = getattr(self.context.get("request"), "user", None)
        with transaction.atomic():
            # yukka kirim qilinadigan bo'lsa soni kirim orqali qo'shiladi
            validated_data["quantity"] = 0 if delivery else quantity
            material = super().create(validated_data)
            if delivery and quantity > 0:
                receive_material_into_delivery(delivery, material, quantity, material.cost_price, "", user)
                material.refresh_from_db()
                # kirim yozuvi allaqachon yaratildi, viewset yana yozmasin
                material.received_via_delivery = True
        return material

    def update(self, instance, validated_data):
        validated_data = self.save_image(validated_data)
        validated_data.pop("delivery", None)
        bunches = validated_data.pop("bunches", None)
        cost_per_bunch = validated_data.pop("cost_per_bunch", None)
        self._apply_bunch_pricing(validated_data, None, cost_per_bunch)
        if bunches:
            per_bunch = int(validated_data.get("units_per_bunch") or instance.units_per_bunch or 20)
            validated_data["quantity"] = int(bunches) * per_bunch
        return super().update(instance, validated_data)


class PackagingMovementSerializer(serializers.ModelSerializer):
    packaging_detail = PackagingSerializer(source="packaging", read_only=True)
    performed_by_detail = UserSerializer(source="performed_by", read_only=True)

    class Meta:
        model = PackagingMovement
        fields = "__all__"
        read_only_fields = ["performed_by"]


class CatalogCompositionListSerializer(serializers.ListSerializer):
    def to_representation(self, data):
        iterable = data.all() if hasattr(data, "all") else data
        rows = [row for row in iterable if int(getattr(row, "quantity_stems", 0) or 0) > 0]
        return super().to_representation(rows)


class CatalogCompositionSerializer(serializers.ModelSerializer):
    batch_detail = StockBatchSerializer(source="stock_batch", read_only=True)

    class Meta:
        model = CatalogComposition
        fields = ["id", "stock_batch", "batch_detail", "quantity_stems", "quantity_bunches"]
        list_serializer_class = CatalogCompositionListSerializer
        # Florist katalogida gul tanlanadi, lekin soni yozilmaydi — u chiqim
        # yopilganda hisoblanadi. Shuning uchun son majburiy emas.
        extra_kwargs = {"quantity_stems": {"required": False, "default": 0}}


class CatalogMaterialUsageSerializer(serializers.ModelSerializer):
    packaging_detail = PackagingSerializer(source="packaging", read_only=True)

    class Meta:
        model = CatalogMaterialUsage
        fields = ["id", "packaging", "packaging_detail", "quantity"]


class CatalogHistorySerializer(serializers.ModelSerializer):
    created_by_detail = UserSerializer(source="created_by", read_only=True)

    class Meta:
        model = CatalogHistory
        fields = "__all__"
        read_only_fields = ["created_by", "created_at", "updated_at"]


class SocialPostCatalogItemSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    composition = CatalogCompositionSerializer(many=True, required=False)
    materials = CatalogMaterialUsageSerializer(many=True, required=False)
    history = CatalogHistorySerializer(many=True, read_only=True)
    florist_detail = FloristProfileSerializer(source="florist", read_only=True)
    decoration_florist_detail = FloristProfileSerializer(source="decoration_florist", read_only=True)
    payment_type = serializers.ChoiceField(choices=["cash", "card"], required=False, write_only=True)

    class Meta:
        model = CatalogItem
        fields = ["id", "name_uz", "description_uz", "description_ru", "note", "arrangement_type", "catalog_kind", "volume", "florist", "florist_detail", "decoration_florist", "decoration_florist_detail", "decoration_salary_amount", "height_cm", "diameter_cm", "price", "florist_fee", "florist_salary_amount", "calculated_component_price", "discount_amount", "discount_percent", "discount_reason", "status", "image_url", "instagram_story_url", "quantity_total", "quantity_sold", "quantity_stock_deducted", "composition", "materials", "history", "payment_type"]
        read_only_fields = ["quantity_sold", "quantity_stock_deducted", "calculated_component_price", "discount_amount", "discount_percent"]


class AICatalogItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = AICatalogItem
        fields = "__all__"
        read_only_fields = ["created_by", "created_at", "updated_at"]

    def validate_name(self, value):
        value = re.sub(r"\s+", " ", (value or "").strip())
        if not value:
            raise serializers.ValidationError("Nomini kiriting")
        parts = []
        for word in value.split(" "):
            parts.append(re.sub(r"(^|[-'‘’`])([a-zа-яё])", lambda match: match.group(1) + match.group(2).upper(), word.lower()))
        return " ".join(parts)


def catalog_stock_error(batch, needed, florist=None):
    """Florist tanlangan bo'lsa gul uning qo'lidagi qoldiqdan olinadi, skladdan emas."""
    if florist is not None:
        row = FloristStockBalance.objects.filter(florist=florist, batch=batch).first()
        remaining = row.remaining_stems if row else 0
        header = f"Katalogni saqlash uchun floristdagi gul yetarli emas. Gul {florist} qo‘lida."
    else:
        remaining = batch.remaining_stems
        header = "Katalogni saqlash uchun sklad qoldig'i yetarli emas."
    missing = max(needed - remaining, 0)
    variant = batch.variant
    flower_name = " ".join(part for part in [variant.flower.name_uz, variant.name_uz, variant.color_uz] if part).strip()
    return (
        f"{header}\n"
        f"Gul: {flower_name}\n"
        f"Partiya: {batch.batch_number}\n"
        f"Kerak: {needed} dona\n"
        f"Bor: {remaining} dona\n"
        f"Yetmayapti: {missing} dona"
    )


def catalog_stock_available(batch, needed, florist=None):
    """Katalog uchun gul yetadimi. Florist tanlangan bo'lsa uning qoldig'i qaraladi."""
    if florist is not None:
        row = FloristStockBalance.objects.filter(florist=florist, batch=batch).first()
        return (row.remaining_stems if row else 0) >= needed
    return batch.remaining_stems >= needed


def catalog_material_error(packaging, needed):
    remaining = packaging.quantity
    missing = max(needed - remaining, 0)
    return (
        "Katalogni saqlash uchun material qoldig'i yetarli emas.\n"
        f"Material: {packaging.name_uz}\n"
        f"Kerak: {needed} dona\n"
        f"Bor: {remaining} dona\n"
        f"Yetmayapti: {missing} dona"
    )


def normalize_catalog_composition_rows(rows):
    grouped = {}
    for row in rows or []:
        batch = row["stock_batch"]
        batch_id = getattr(batch, "id", batch)
        if batch_id not in grouped:
            grouped[batch_id] = {"stock_batch": batch, "quantity_stems": 0, "quantity_bunches": Decimal("0")}
        grouped[batch_id]["quantity_stems"] += row.get("quantity_stems") or 0
        grouped[batch_id]["quantity_bunches"] += Decimal(str(row.get("quantity_bunches") or 0))
    return list(grouped.values())


def normalize_catalog_material_rows(rows):
    grouped = {}
    for row in rows or []:
        packaging = row["packaging"]
        packaging_id = getattr(packaging, "id", packaging)
        if packaging_id not in grouped:
            grouped[packaging_id] = {"packaging": packaging, "quantity": 0}
        grouped[packaging_id]["quantity"] += row.get("quantity") or 1
    return list(grouped.values())


def catalog_payload_merge_key(item):
    if item.get("catalog_kind") == "custom" and not item.get("id"):
        return ("custom", item.get("arrangement_type") or "", item.get("florist") or "")
    return (
        item.get("id") or "",
        item.get("catalog_kind") or "standard",
        item.get("arrangement_type") or "",
        item.get("volume") or "",
        item.get("name_uz") or "",
        str(item.get("price") or ""),
        item.get("florist") or "",
    )


def merge_catalog_item_payloads(items):
    merged = []
    by_key = {}
    for raw in items or []:
        item = dict(raw)
        item["composition"] = normalize_catalog_composition_rows(item.get("composition") or [])
        item["materials"] = normalize_catalog_material_rows(item.get("materials") or [])
        key = catalog_payload_merge_key(item)
        current = by_key.get(key)
        if not current:
            by_key[key] = item
            merged.append(item)
            continue
        names = [name for name in [current.get("name_uz"), item.get("name_uz")] if name]
        unique_names = []
        for name in names:
            if name not in unique_names:
                unique_names.append(name)
        if unique_names:
            current["name_uz"] = " + ".join(unique_names)
        if item.get("description_uz"):
            current["description_uz"] = "\n".join(part for part in [current.get("description_uz"), item.get("description_uz")] if part)
        if item.get("description_ru"):
            current["description_ru"] = "\n".join(part for part in [current.get("description_ru"), item.get("description_ru")] if part)
        if item.get("discount_reason"):
            current["discount_reason"] = "\n".join(part for part in [current.get("discount_reason"), item.get("discount_reason")] if part)
        if item.get("price") is not None and current.get("catalog_kind") == "custom":
            current["price"] = Decimal(str(current.get("price") or 0)) + Decimal(str(item.get("price") or 0))
        if item.get("florist_salary_amount") is not None and current.get("catalog_kind") == "custom":
            current["florist_salary_amount"] = Decimal(str(current.get("florist_salary_amount") or 0)) + Decimal(str(item.get("florist_salary_amount") or 0))
        current["quantity_total"] = max(current.get("quantity_total") or 1, item.get("quantity_total") or 1)
        for field in ["image_url", "instagram_story_url", "volume", "florist", "height_cm", "diameter_cm", "florist_salary_amount"]:
            if not current.get(field) and item.get(field):
                current[field] = item[field]
        current["composition"] = normalize_catalog_composition_rows((current.get("composition") or []) + (item.get("composition") or []))
        current["materials"] = normalize_catalog_material_rows((current.get("materials") or []) + (item.get("materials") or []))
    return merged


def apply_volume_rate_to_attrs(attrs, initial_data=None, default_kind="standard"):
    data = initial_data or {}
    kind = attrs.get("catalog_kind") or data.get("catalog_kind") or default_kind
    arrangement_type = attrs.get("arrangement_type") or data.get("arrangement_type")
    florist = attrs.get("florist")
    if florist and florist.staff_type != "florist":
        attrs["florist_salary_amount"] = Decimal("0")
        attrs.pop("_volume_default_stems", None)
        return attrs
    if kind == "standard":
        attrs.pop("florist_salary_amount", None)
    elif "florist_salary_amount" in attrs or "florist_salary_amount" in data:
        return attrs
    volume = attrs.get("volume")
    if arrangement_type and volume and florist:
        rate = FloristVolumeRate.objects.filter(florist=florist, arrangement_type=arrangement_type, volume=volume, is_active=True).first()
        if rate:
            attrs["florist_salary_amount"] = rate.florist_fee
            if rate.default_stems:
                attrs.setdefault("_volume_default_stems", rate.default_stems)
                attrs.pop("_volume_default_stems", None)
    return attrs


def validate_catalog_discount_reason(item):
    if item.catalog_kind == "custom" and item.discount_amount > 0 and not (item.discount_reason or "").strip():
        raise serializers.ValidationError({"discount_reason": "Custom katalog sotuv narxi hisoblangan narxdan arzon bo‘lsa, skidka sababi majburiy"})


def custom_component_unit_price(item):
    quantity = Decimal(item.quantity_total or 1)
    if quantity <= 0:
        return Decimal("0")
    return (Decimal(item.calculated_component_price or 0) / quantity).quantize(Decimal("0.01"))


class SocialPostSerializer(serializers.ModelSerializer):
    reply_count = serializers.IntegerField(read_only=True)
    lead_count = serializers.IntegerField(read_only=True)
    leads = serializers.SerializerMethodField()
    catalog_items = SocialPostCatalogItemSerializer(many=True, required=False)
    class Meta:
        model = SocialPost
        fields = "__all__"
        extra_kwargs = {"media_id": {"required": False}, "post_type": {"required": False}}

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_leads(self, obj):
        rows = obj.leads.select_related("customer").prefetch_related("catalog_usage__catalog_item").order_by("-created_at")[:50]
        return [{
            "id": row.id,
            "status": row.status,
            "customer": row.customer_id,
            "customer_name": row.customer.name,
            "customer_phone": row.customer.phone,
            "customer_instagram_user_id": row.customer.instagram_user_id,
            "request_uz": row.request_uz,
            "request_ru": row.request_ru,
            "arrangement_type": row.arrangement_type,
            "estimated_price": str(row.estimated_price) if row.estimated_price is not None else None,
            "source": row.source,
            "created_at": row.created_at.isoformat(),
            "catalog_items": [{"id": usage.catalog_item_id, "name_uz": usage.catalog_item.name_uz, "quantity": usage.quantity} for usage in row.catalog_usage.all()],
        } for row in rows]

    def _fill_story_share_fields(self, validated_data):
        permalink = validated_data.get("permalink") or getattr(self.instance, "permalink", "")
        if not permalink:
            return validated_data
        parsed = urlparse(permalink)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "stories":
            validated_data.setdefault("instagram_username", parts[1])
            validated_data.setdefault("story_share_id", parts[2])
            validated_data.setdefault("post_type", "story")
            if not validated_data.get("media_id"):
                validated_data["media_id"] = f"story-share-{parts[2]}"
            if not validated_data.get("webhook_story_id"):
                try:
                    from .platform_services import find_active_story_by_permalink
                    story = find_active_story_by_permalink(permalink)
                    if story:
                        validated_data["webhook_story_id"] = story.get("id", "")
                        validated_data["webhook_story_url"] = story.get("media_url", "")
                        if story.get("media_url") and len(story["media_url"]) <= 200 and not validated_data.get("image_url"):
                            validated_data["image_url"] = story["media_url"]
                except Exception:
                    pass
        elif not validated_data.get("media_id"):
            try:
                from .platform_services import find_media_by_permalink
                media = find_media_by_permalink(permalink)
                if media:
                    validated_data["media_id"] = media.get("id", "")
                    media_type = (media.get("media_type") or "").lower()
                    if media_type == "video":
                        validated_data.setdefault("post_type", "reel")
                    else:
                        validated_data.setdefault("post_type", "post")
                    media_url = media.get("thumbnail_url") or media.get("media_url")
                    if media_url and len(media_url) <= 200 and not validated_data.get("image_url"):
                        validated_data["image_url"] = media_url
            except Exception:
                pass
            if not validated_data.get("media_id") and parts:
                validated_data["media_id"] = f"post-link-{parts[-1]}"
                validated_data.setdefault("post_type", "reel" if parts[0] in ["reel", "reels"] else "post")
        return validated_data

    def _check_unique_media_id(self, validated_data):
        media_id = validated_data.get("media_id")
        if media_id:
            queryset = SocialPost.objects.filter(media_id=media_id)
            if self.instance:
                queryset = queryset.exclude(pk=self.instance.pk)
            existing = queryset.first()
            if existing:
                raise serializers.ValidationError({"media_id": f"Bu Instagram media allaqachon SocialPost id={existing.id} da bor."})
        permalink = validated_data.get("permalink") or getattr(self.instance, "permalink", "")
        normalized = permalink.split("?")[0].rstrip("/") if permalink else ""
        if normalized:
            queryset = SocialPost.objects.filter(permalink__startswith=normalized)
            if self.instance:
                queryset = queryset.exclude(pk=self.instance.pk)
            existing = queryset.first()
            if existing:
                raise serializers.ValidationError({"permalink": f"Bu Instagram link allaqachon SocialPost id={existing.id} da bor."})

    def _validate_catalog_items(self, post_data, catalog_items):
        for item in catalog_items:
            quantity_total = item.get("quantity_total", 1)
            florist = item.get("florist")
            stock_florist = florist if item.get("catalog_kind", "standard") != "custom" else None
            for row in item.get("composition") or []:
                batch = row["stock_batch"]
                needed = row["quantity_stems"] * quantity_total
                if not catalog_stock_available(batch, needed, stock_florist):
                    detail = catalog_stock_error(batch, needed, stock_florist)
                    raise DetailValidationError(detail)
            for row in item.get("materials") or []:
                packaging = row["packaging"]
                needed = row.get("quantity", 1) * quantity_total
                if packaging.quantity < needed:
                    detail = catalog_material_error(packaging, needed)
                    raise DetailValidationError(detail)

    def validate(self, attrs):
        if "catalog_items" in attrs:
            attrs["catalog_items"] = merge_catalog_item_payloads(attrs.get("catalog_items") or [])
        catalog_items = attrs.get("catalog_items") or []
        self._validate_catalog_items(attrs, catalog_items)
        return attrs

    def _sync_catalog_items(self, post, catalog_items):
        from .inventory_services import create_catalog_history, deduct_catalog_inventory, notify_florist_catalog, restore_catalog_inventory, sync_catalog_decoration_salary, sync_catalog_financials, sync_catalog_florist_salary
        user = getattr(self.context.get("request"), "user", None)
        for item_data in merge_catalog_item_payloads(catalog_items):
            item_data.pop("payment_type", "")
            has_composition = "composition" in item_data
            composition = item_data.pop("composition", None)
            has_materials = "materials" in item_data
            materials = item_data.pop("materials", None)
            item_id = item_data.pop("id", None)
            old_florist_id = None
            if not item_data.get("image_url") and post.image_url:
                item_data["image_url"] = post.image_url
            if not item_data.get("instagram_story_url") and post.post_type == "story" and post.permalink:
                item_data["instagram_story_url"] = post.permalink
            if item_id:
                item = post.catalog_items.get(id=item_id)
                old_florist_id = item.florist_id
                old_quantity_total = item.quantity_total
                if item.quantity_sold and (has_composition or has_materials):
                    raise serializers.ValidationError({"catalog_items": "Sotilgan katalog tarkibini o‘zgartirib bo‘lmaydi"})
                if has_composition or has_materials:
                    restore_catalog_inventory(item, user, item.quantity_stock_deducted)
                    item.refresh_from_db()
                for key, value in item_data.items():
                    setattr(item, key, value)
                item.social_post = post
                item.save()
            else:
                item_data = apply_volume_rate_to_attrs(item_data, item_data)
                item = CatalogItem.objects.create(social_post=post, **item_data)
            if has_composition:
                item.composition.all().delete()
                CatalogComposition.objects.bulk_create([CatalogComposition(catalog_item=item, **row) for row in composition])
            if has_materials:
                item.materials.all().delete()
                CatalogMaterialUsage.objects.bulk_create([CatalogMaterialUsage(catalog_item=item, **row) for row in materials])
            if item_id and (has_composition or has_materials):
                deduct_catalog_inventory(item, user, item.quantity_total)
            elif item_id:
                delta = item.quantity_total - old_quantity_total
                if delta > 0:
                    deduct_catalog_inventory(item, user, delta)
                elif delta < 0:
                    restore_catalog_inventory(item, user, abs(delta))
            elif not item_id:
                deduct_catalog_inventory(item, user, item.quantity_total)
            item = sync_catalog_financials(item)
            validate_catalog_discount_reason(item)
            sync_catalog_florist_salary(item, user)
            sync_catalog_decoration_salary(item, user)
            if item.florist_id and (not item_id or item.florist_id != old_florist_id):
                notify_florist_catalog(item, "Yangi ish biriktirildi", f"{item.name_uz} katalogi sizga biriktirildi.")
            if item.catalog_kind == "custom":
                if not item.history.filter(action="created").exists():
                    create_catalog_history(item, "created", user=user, note="Custom katalog qo‘shildi")
            elif not item.history.filter(action="created").exists():
                create_catalog_history(item, "created", user=user, note="Katalog qo‘shildi")

    def create(self, validated_data):
        catalog_items = validated_data.pop("catalog_items", [])
        validated_data = self._fill_story_share_fields(validated_data)
        self._check_unique_media_id(validated_data)
        with transaction.atomic():
            post = super().create(validated_data)
            self._sync_catalog_items(post, catalog_items)
        return post

    def update(self, instance, validated_data):
        catalog_items = validated_data.pop("catalog_items", None)
        validated_data = self._fill_story_share_fields(validated_data)
        self._check_unique_media_id(validated_data)
        with transaction.atomic():
            post = super().update(instance, validated_data)
            if catalog_items is not None:
                self._sync_catalog_items(post, catalog_items)
        return post


class BranchSerializer(serializers.ModelSerializer):
    # Bot tokeni javobda qaytmaydi — faqat yozish uchun.
    sale_group_configured = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Branch
        fields = "__all__"
        extra_kwargs = {
            "sale_bot_token": {"write_only": True, "required": False, "allow_blank": True},
            "sale_group_chat_id": {"write_only": True, "required": False, "allow_blank": True},
        }

    @extend_schema_field(serializers.BooleanField())
    def get_sale_group_configured(self, obj):
        """Sotuv xabari uchun bot va guruh sozlanganmi."""
        return bool((obj.sale_bot_token or "").strip() and (obj.sale_group_chat_id or "").strip())


def request_user_branch(serializer):
    """So'rov yuborayotgan foydalanuvchi filialga biriktirilganmi."""
    user = getattr(serializer.context.get("request"), "user", None)
    return getattr(getattr(user, "profile", None), "branch_id", None)


# Filial faqat o'z sotuv narxini biladi. Asosiy filialning narxi, tannarxi va
# foydasi unga ko'rinmasligi kerak.
BRANCH_HIDDEN_CATALOG_FIELDS = [
    "source_price", "source_item",
    "calculated_cost_price", "calculated_component_price",
    "florist_fee", "florist_salary_amount",
    "discount_amount", "discount_percent",
    "profit", "florist", "florist_detail",
]
BRANCH_HIDDEN_BATCH_FIELDS = [
    "cost_per_stem", "cost_per_bunch", "cost_per_stem_exact",
    "sale_price_per_stem", "sale_price_per_bunch", "sale_price_per_stem_exact",
    "stock_value", "rounding", "supplier", "supplier_detail", "delivery", "delivery_detail",
    "received_stems", "remaining_stems", "remaining_bunches", "remaining_bunches_label",
]


def strip_branch_sensitive_catalog(data):
    """Katalog javobidan asosiy filialga tegishli pul ma'lumotlarini olib tashlaydi."""
    for key in BRANCH_HIDDEN_CATALOG_FIELDS:
        data.pop(key, None)
    for row in data.get("composition") or []:
        batch = row.get("batch_detail")
        if isinstance(batch, dict):
            for key in BRANCH_HIDDEN_BATCH_FIELDS:
                batch.pop(key, None)
    for row in data.get("materials") or []:
        material = row.get("packaging_detail")
        if isinstance(material, dict):
            for key in ["cost_price", "sale_price", "quantity", "quantity_label", "last_delivery"]:
                material.pop(key, None)
    for row in data.get("history") or []:
        # snapshot ichida asosiy filial narxi va tarkibi turadi
        row.pop("snapshot", None)
    return data


class CatalogTransferSerializer(serializers.ModelSerializer):
    branch_name = serializers.SerializerMethodField(read_only=True)
    catalog_name = serializers.SerializerMethodField(read_only=True)
    created_by_detail = UserSerializer(source="created_by", read_only=True)

    class Meta:
        model = CatalogTransfer
        fields = "__all__"
        read_only_fields = ["created_by"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if request_user_branch(self):
            # filial o'ziga kelgan sonni ko'radi, asosiy filial narxini emas
            data.pop("source_price", None)
            data.pop("source_item", None)
        return data

    @extend_schema_field(serializers.CharField())
    def get_branch_name(self, obj):
        return obj.branch.name

    @extend_schema_field(serializers.CharField())
    def get_catalog_name(self, obj):
        return obj.target_item.name_uz


class CatalogTransferRequestSerializer(serializers.Serializer):
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.filter(is_active=True, is_main=False))
    quantity = serializers.IntegerField(min_value=1)
    price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    note = serializers.CharField(required=False, allow_blank=True)


class CatalogListBatchSerializer(serializers.ModelSerializer):
    flower_name = serializers.CharField(read_only=True)
    title = serializers.CharField(read_only=True)
    height_label = serializers.CharField(read_only=True)
    variant_detail = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = StockBatch
        fields = ["id", "batch_number", "variant", "variant_detail", "flower_name", "title", "height_label", "height_cm", "stems_per_bunch", "image_url"]

    @extend_schema_field(serializers.DictField())
    def get_variant_detail(self, obj):
        variant = obj.variant
        return {
            "id": variant.id,
            "flower": variant.flower_id,
            "name_uz": variant.name_uz,
            "color_uz": variant.color_uz,
            "flower_detail": {"id": variant.flower_id, "name_uz": variant.flower.name_uz, "image_url": variant.flower.image_url},
        }


class CatalogListCompositionSerializer(serializers.ModelSerializer):
    batch_detail = CatalogListBatchSerializer(source="stock_batch", read_only=True)

    class Meta:
        model = CatalogComposition
        fields = ["id", "stock_batch", "batch_detail", "quantity_stems", "quantity_bunches"]
        list_serializer_class = CatalogCompositionListSerializer


class CatalogListMaterialSerializer(serializers.ModelSerializer):
    packaging_detail = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = CatalogMaterialUsage
        fields = ["id", "packaging", "packaging_detail", "quantity"]

    @extend_schema_field(serializers.DictField())
    def get_packaging_detail(self, obj):
        packaging = obj.packaging
        return {
            "id": packaging.id,
            "name_uz": packaging.name_uz,
            "packaging_type": packaging.packaging_type,
            "packaging_type_label": packaging.get_packaging_type_display(),
            "size": packaging.size,
            "image_url": packaging.image_url,
        }


class CatalogItemListSerializer(serializers.ModelSerializer):
    florist = serializers.SerializerMethodField(read_only=True)
    florist_name = serializers.SerializerMethodField(read_only=True)
    florist_detail = serializers.SerializerMethodField(read_only=True)
    decoration_florist = serializers.SerializerMethodField(read_only=True)
    decoration_florist_name = serializers.SerializerMethodField(read_only=True)
    decoration_florist_detail = serializers.SerializerMethodField(read_only=True)
    customer_detail = serializers.SerializerMethodField(read_only=True)
    branch_name = serializers.SerializerMethodField(read_only=True)
    quantity_remaining = serializers.SerializerMethodField(read_only=True)
    profit = serializers.SerializerMethodField(read_only=True)
    composition = CatalogListCompositionSerializer(many=True, read_only=True)
    materials = CatalogListMaterialSerializer(many=True, read_only=True)

    class Meta:
        model = CatalogItem
        fields = [
            "id", "name_uz", "arrangement_type", "catalog_kind", "volume", "branch", "branch_name",
            "customer", "customer_detail", "florist", "florist_name", "florist_detail", "decoration_florist",
            "decoration_florist_name", "decoration_florist_detail", "height_cm", "diameter_cm", "price", "calculated_cost_price",
            "discount_amount", "discount_percent", "status", "image_url", "instagram_story_url",
            "quantity_total", "quantity_sold", "quantity_wasted", "quantity_reworked",
            "quantity_remaining", "sold_at", "created_at", "updated_at", "profit",
            "composition", "materials",
        ]

    @extend_schema_field(serializers.CharField())
    def get_branch_name(self, obj):
        return obj.branch.name if obj.branch_id else "Asosiy filial"

    @extend_schema_field(serializers.CharField())
    def get_florist(self, obj):
        if not obj.florist_id:
            return ""
        return str(obj.florist)

    @extend_schema_field(serializers.CharField())
    def get_florist_name(self, obj):
        if not obj.florist_id:
            return ""
        return str(obj.florist)

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_florist_detail(self, obj):
        return self._florist_payload(obj.florist) if obj.florist_id else None

    @extend_schema_field(serializers.CharField())
    def get_decoration_florist(self, obj):
        if not obj.decoration_florist_id:
            return ""
        return str(obj.decoration_florist)

    @extend_schema_field(serializers.CharField())
    def get_decoration_florist_name(self, obj):
        if not obj.decoration_florist_id:
            return ""
        return str(obj.decoration_florist)

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_decoration_florist_detail(self, obj):
        return self._florist_payload(obj.decoration_florist) if obj.decoration_florist_id else None

    def _florist_payload(self, florist):
        return {
            "id": florist.id,
            "name": str(florist),
            "staff_type": florist.staff_type,
            "staff_type_label": florist.get_staff_type_display(),
            "phone": florist.phone,
            "user": florist.user_id,
        }

    @extend_schema_field(serializers.IntegerField())
    def get_quantity_remaining(self, obj):
        return max(int(obj.quantity_total or 0) - int(obj.quantity_sold or 0) - int(obj.quantity_wasted or 0) - int(obj.quantity_reworked or 0), 0)

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_customer_detail(self, obj):
        if not obj.customer_id:
            return None
        return {"id": obj.customer_id, "name": obj.customer.name, "phone": obj.customer.phone, "masked_phone": obj.customer.masked_phone}

    @extend_schema_field(serializers.DictField())
    def get_profit(self, obj):
        total = Decimal(obj.quantity_total or 1)
        if total <= 0:
            total = Decimal("1")
        cost_total = Decimal(obj.calculated_cost_price or 0)
        price = Decimal(obj.price or 0)
        unit_cost = (cost_total / total).quantize(Decimal("0.01"))
        unit_profit = (price - unit_cost).quantize(Decimal("0.01"))
        margin = (unit_profit / price * 100).quantize(Decimal("0.01")) if price else Decimal("0")
        sold = Decimal(obj.quantity_sold or 0)
        return {
            "unit_price": str(price),
            "unit_cost": str(unit_cost),
            "unit_profit": str(unit_profit),
            "unit_margin_percent": str(margin),
            "realized_profit": str((unit_profit * sold).quantize(Decimal("0.01"))),
        }

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if request_user_branch(self):
            data = strip_branch_sensitive_catalog(data)
        return data


class CatalogItemSerializer(serializers.ModelSerializer):
    composition = CatalogCompositionSerializer(many=True, required=False)
    materials = CatalogMaterialUsageSerializer(many=True, required=False)
    history = CatalogHistorySerializer(many=True, read_only=True)
    social_post_detail = SocialPostSerializer(source="social_post", read_only=True)
    florist_detail = FloristProfileSerializer(source="florist", read_only=True)
    decoration_florist_detail = FloristProfileSerializer(source="decoration_florist", read_only=True)
    customer_detail = serializers.SerializerMethodField(read_only=True)
    branch_name = serializers.SerializerMethodField(read_only=True)
    quantity_remaining = serializers.SerializerMethodField(read_only=True)
    profit = serializers.SerializerMethodField(read_only=True)
    customer_name = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=160)
    customer_phone = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=30)
    payment_type = serializers.ChoiceField(choices=["cash", "card"], required=False, write_only=True)
    created_at = serializers.DateTimeField(
        required=False,
        help_text="Katalog qachon qo‘shilgani. O‘tib ketgan kun uchun belgilanadi, berilmasa hozirgi vaqt.",
    )

    class Meta:
        model = CatalogItem
        fields = "__all__"
        read_only_fields = ["created_by", "sold_at", "stock_deducted_at", "calculated_component_price", "discount_amount", "discount_percent"]

    @extend_schema_field(serializers.CharField())
    def get_branch_name(self, obj):
        return obj.branch.name if obj.branch_id else "Asosiy filial"

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if request_user_branch(self):
            data = strip_branch_sensitive_catalog(data)
        return data

    @extend_schema_field(serializers.IntegerField())
    def get_quantity_remaining(self, obj):
        """Sotilmagan va chiqitga chiqmagan dona soni."""
        return max(int(obj.quantity_total or 0) - int(obj.quantity_sold or 0) - int(obj.quantity_wasted or 0), 0)

    @extend_schema_field(serializers.DictField())
    def get_profit(self, obj):
        """Bitta dona uchun va jami bo'yicha sof foyda."""
        total = Decimal(obj.quantity_total or 1)
        if total <= 0:
            total = Decimal("1")
        cost_total = Decimal(obj.calculated_cost_price or 0)
        price = Decimal(obj.price or 0)
        unit_cost = (cost_total / total).quantize(Decimal("0.01"))
        unit_profit = (price - unit_cost).quantize(Decimal("0.01"))
        margin = (unit_profit / price * 100).quantize(Decimal("0.01")) if price else Decimal("0")
        sold = Decimal(obj.quantity_sold or 0)
        return {
            "unit_price": str(price),
            "unit_cost": str(unit_cost),
            "unit_profit": str(unit_profit),
            "unit_margin_percent": str(margin),
            "total_cost": str(cost_total),
            "total_potential_profit": str((unit_profit * total).quantize(Decimal("0.01"))),
            "sold_quantity": int(sold),
            "realized_profit": str((unit_profit * sold).quantize(Decimal("0.01"))),
        }

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_customer_detail(self, obj):
        if not obj.customer_id:
            return None
        customer = obj.customer
        return {
            "id": customer.id,
            "name": customer.name,
            "phone": customer.phone,
            "masked_phone": customer.masked_phone,
            "instagram_username": customer.instagram_username,
            "instagram_user_id": customer.instagram_user_id,
        }

    def validate(self, attrs):
        composition = attrs.get("composition")
        materials = attrs.get("materials")
        quantity_total = attrs.get("quantity_total", getattr(self.instance, "quantity_total", 1))
        if self.instance and self.instance.quantity_sold and (composition is not None or materials is not None):
            raise serializers.ValidationError({"composition": "Sotilgan katalog tarkibini o‘zgartirib bo‘lmaydi"})
        if composition is None and self.instance:
            composition = [{"stock_batch": row.stock_batch, "quantity_stems": row.quantity_stems} for row in self.instance.composition.select_related("stock_batch")]
        if materials is None and self.instance:
            materials = [{"packaging": row.packaging, "quantity": row.quantity} for row in self.instance.materials.select_related("packaging")]
        if "composition" in attrs:
            attrs["composition"] = normalize_catalog_composition_rows(composition)
            composition = attrs["composition"]
        elif composition is not None:
            composition = normalize_catalog_composition_rows(composition)
        if "materials" in attrs:
            attrs["materials"] = normalize_catalog_material_rows(materials)
            materials = attrs["materials"]
        elif materials is not None:
            materials = normalize_catalog_material_rows(materials)
        florist_value = attrs.get("florist", getattr(self.instance, "florist", None))
        kind = attrs.get("catalog_kind") or getattr(self.instance, "catalog_kind", None) or "standard"
        arrangement_type = attrs.get("arrangement_type") or getattr(self.instance, "arrangement_type", None)
        volume = attrs.get("volume") or getattr(self.instance, "volume", None)
        stock_florist = florist_value if kind != "custom" else None
        if florist_value and not self.instance:
            if not arrangement_type:
                raise serializers.ValidationError({"arrangement_type": "Florist katalogida turini tanlash kerak"})
            if not (volume or "").strip():
                raise serializers.ValidationError({"volume": "Florist katalogida hajmni tanlash kerak — gul shu bo‘yicha taqsimlanadi"})
            if not composition:
                raise serializers.ValidationError({"composition": "Floristga chiqarilgan qaysi guldan yasalganini tanlang"})
        if kind == "standard" and florist_value and florist_value.staff_type == "florist":
            if arrangement_type and volume and not FloristVolumeRate.objects.filter(
                florist=florist_value, arrangement_type=arrangement_type, volume=volume, is_active=True,
            ).exists():
                raise serializers.ValidationError({
                    "volume": f"{florist_value} uchun bu hajm tarifi belgilanmagan. Avval floristga hajm narxini kiriting.",
                })
        if composition and not stock_florist:
            for row in composition:
                if int(row.get("quantity_stems") or 0) < 1:
                    raise serializers.ValidationError({"composition": "Gul sonini kiriting"})
        if composition and not self.instance:
            for row in composition:
                batch = row["stock_batch"]
                needed = row["quantity_stems"] * quantity_total
                if not catalog_stock_available(batch, needed, stock_florist):
                    detail = catalog_stock_error(batch, needed, stock_florist)
                    raise DetailValidationError(detail)
        if materials and not self.instance:
            for row in materials:
                packaging = row["packaging"]
                needed = row.get("quantity", 1) * quantity_total
                if packaging.quantity < needed:
                    detail = catalog_material_error(packaging, needed)
                    raise DetailValidationError(detail)
        quantity_sold = getattr(self.instance, "quantity_sold", 0)
        if quantity_total < quantity_sold:
            raise serializers.ValidationError({"quantity_total": "Umumiy son sotilgan sondan kam bo‘lishi mumkin emas"})
        return attrs

    def create(self, validated_data):
        from .inventory_services import create_catalog_history, deduct_catalog_inventory, notify_florist_catalog, sync_catalog_decoration_salary, sync_catalog_financials, sync_catalog_florist_salary
        validated_data.pop("payment_type", "")
        validated_data["customer"] = resolve_or_create_customer(
            customer=validated_data.pop("customer", None),
            name=validated_data.pop("customer_name", ""),
            phone=validated_data.pop("customer_phone", ""),
        )
        composition = normalize_catalog_composition_rows(validated_data.pop("composition", []))
        materials = normalize_catalog_material_rows(validated_data.pop("materials", []))
        created_at = validated_data.pop("created_at", None)
        validated_data = apply_volume_rate_to_attrs(validated_data, getattr(self, "initial_data", {}))
        validated_data = self._sync_social_post_image_data(validated_data)
        user = getattr(self.context.get("request"), "user", None)
        with transaction.atomic():
            item = CatalogItem.objects.create(**validated_data)
            CatalogComposition.objects.bulk_create([CatalogComposition(catalog_item=item, **row) for row in composition])
            CatalogMaterialUsage.objects.bulk_create([CatalogMaterialUsage(catalog_item=item, **row) for row in materials])
            try:
                deduct_catalog_inventory(item, user, item.quantity_total)
            except ValueError as exc:
                raise serializers.ValidationError({"detail": str(exc)})
            item = sync_catalog_financials(item)
            # To'g'ridan-to'g'ri filial uchun qo'shilgan katalogda "kelib chiqish narxi"
            # bo'lmaydi — gul asosiy filial skladidan ketgan. Shuning uchun uning
            # o'rniga bir donaga to'g'ri keladigan tannarx yoziladi va filial
            # hisobotidagi ustama haqiqiy foydani ko'rsatadi.
            if item.branch_id and item.source_price is None:
                units = Decimal(item.quantity_total or 1)
                item.source_price = (Decimal(item.calculated_cost_price or 0) / units).quantize(Decimal("0.01")) if units else Decimal("0")
                item.save(update_fields=["source_price", "updated_at"])
            validate_catalog_discount_reason(item)
            sync_catalog_florist_salary(item, user)
            sync_catalog_decoration_salary(item, user)
            create_catalog_history(item, "created", user=user, note="Custom katalog qo‘shildi" if item.catalog_kind == "custom" else "Katalog qo‘shildi")
            notify_florist_catalog(item, "Yangi ish biriktirildi", f"{item.name_uz} katalogi sizga biriktirildi.")
            self._sync_social_post_image(item)
            if created_at:
                # katalog o'tib ketgan kunga yozilsa, tarix va florist ish haqi
                # sanasi ham o'sha kunga qo'yiladi
                backdate_record(item, created_at)
                CatalogHistory.objects.filter(catalog_item=item).update(created_at=created_at)
                FloristSalaryEntry.objects.filter(catalog_item=item).update(work_date=timezone.localtime(created_at).date())
        return item

    def update(self, instance, validated_data):
        from .inventory_services import create_catalog_history, deduct_catalog_inventory, notify_florist_catalog, restore_catalog_inventory, sync_catalog_decoration_salary, sync_catalog_financials, sync_catalog_florist_salary
        validated_data.pop("payment_type", "")
        customer_name = validated_data.pop("customer_name", "")
        customer_phone = validated_data.pop("customer_phone", "")
        if "customer" in validated_data or customer_name or customer_phone:
            resolved = resolve_or_create_customer(
                customer=validated_data.pop("customer", None),
                name=customer_name,
                phone=customer_phone,
            )
            validated_data["customer"] = resolved
        old_florist_id = instance.florist_id
        edited_created_at = validated_data.pop("created_at", None)
        composition = validated_data.pop("composition", None)
        target_kind = validated_data.get("catalog_kind") or instance.catalog_kind
        if composition is not None and target_kind != "custom" and (validated_data.get("florist") or instance.florist_id):
            existing = {row.stock_batch_id: row.quantity_stems for row in instance.composition.all()}
            for row in composition:
                batch = row.get("stock_batch")
                batch_id = getattr(batch, "id", batch)
                if not row.get("quantity_stems") and existing.get(batch_id):
                    row["quantity_stems"] = existing[batch_id]
        materials = validated_data.pop("materials", None)
        if composition is not None:
            composition = normalize_catalog_composition_rows(composition)
        if materials is not None:
            materials = normalize_catalog_material_rows(materials)
        initial_data = dict(getattr(self, "initial_data", {}) or {})
        initial_data.setdefault("arrangement_type", instance.arrangement_type)
        validated_data = apply_volume_rate_to_attrs(validated_data, initial_data, instance.catalog_kind)
        validated_data = self._sync_social_post_image_data(validated_data)
        user = getattr(self.context.get("request"), "user", None)
        old_quantity_total = instance.quantity_total
        with transaction.atomic():
            if composition is not None or materials is not None:
                restore_catalog_inventory(instance, user, instance.quantity_stock_deducted)
                # qoldiq qaytarilganda quantity_stock_deducted bazada o'zgaradi.
                # Obyekt yangilanmasa keyingi save eski qiymatni qaytarib yozadi
                # va "sondan oshib ketdi" xatosi chiqadi.
                instance.refresh_from_db()
            instance = super().update(instance, validated_data)
            if composition is not None:
                instance.composition.all().delete()
                CatalogComposition.objects.bulk_create([CatalogComposition(catalog_item=instance, **row) for row in composition])
            if materials is not None:
                instance.materials.all().delete()
                CatalogMaterialUsage.objects.bulk_create([CatalogMaterialUsage(catalog_item=instance, **row) for row in materials])
            try:
                if composition is not None or materials is not None:
                    deduct_catalog_inventory(instance, user, instance.quantity_total)
                else:
                    delta = instance.quantity_total - old_quantity_total
                    if delta > 0:
                        deduct_catalog_inventory(instance, user, delta)
                    elif delta < 0:
                        restore_catalog_inventory(instance, user, abs(delta))
            except ValueError as exc:
                raise serializers.ValidationError({"detail": str(exc)})
            instance = sync_catalog_financials(instance)
            validate_catalog_discount_reason(instance)
            sync_catalog_florist_salary(instance, user)
            if edited_created_at:
                backdate_record(instance, edited_created_at)
                CatalogHistory.objects.filter(catalog_item=instance, action="created").update(created_at=edited_created_at)
                FloristSalaryEntry.objects.filter(catalog_item=instance).update(work_date=timezone.localtime(edited_created_at).date())
            sync_catalog_decoration_salary(instance, user)
            if instance.florist_id and instance.florist_id != old_florist_id:
                notify_florist_catalog(instance, "Yangi ish biriktirildi", f"{instance.name_uz} katalogi sizga biriktirildi.")
            create_catalog_history(instance, "updated", user=user, note="Katalog o‘zgartirildi")
            self._sync_social_post_image(instance)
        return instance

    def _sync_social_post_image_data(self, validated_data):
        social_post = validated_data.get("social_post") or getattr(self.instance, "social_post", None)
        if social_post and not validated_data.get("image_url") and getattr(social_post, "image_url", ""):
            validated_data["image_url"] = social_post.image_url
        return validated_data

    def _sync_social_post_image(self, item):
        if item.social_post_id and item.image_url and item.social_post.image_url != item.image_url:
            item.social_post.image_url = item.image_url
            item.social_post.save(update_fields=["image_url", "updated_at"])


def backdate_record(instance, created_at=None):
    """auto_now_add tufayli created_at yozilmaydi. Tarixiy ma'lumot kiritish uchun
    yozuv yaratilgandan keyin to'g'ridan-to'g'ri UPDATE qilamiz."""
    if not created_at:
        return instance
    type(instance).objects.filter(pk=instance.pk).update(created_at=created_at)
    instance.created_at = created_at
    return instance


def resolve_or_create_customer(customer=None, name="", phone="", external_id=""):
    """Mijozni topadi yoki yaratadi. Tayyor customer berilsa o'shani qaytaradi.
    Telefon bo'yicha mavjud mijoz topilsa yangi yaratmaydi, ismini to'ldiradi."""
    if customer:
        return customer
    from .services import normalize_phone
    name = (name or "").strip()
    normalized = normalize_phone(phone) or (phone or "").strip()
    if not normalized and not name and not external_id:
        return None
    existing = Customer.objects.filter(phone=normalized).first() if normalized else None
    if not existing and external_id:
        existing = Customer.objects.filter(instagram_user_id=external_id).first()
    if existing:
        updates = []
        if name and not existing.name:
            existing.name = name[:160]
            updates.append("name")
        if normalized and not existing.phone:
            existing.phone = normalized
            updates.append("phone")
        if updates:
            existing.save(update_fields=updates + ["updated_at"])
        return existing
    external = external_id or f"manual:{normalized or name}"
    if Customer.objects.filter(instagram_user_id=external).exists():
        external = f"{external}:{timezone.now().timestamp():.0f}"
    return Customer.objects.create(name=name[:160], phone=normalized, language="uz", instagram_user_id=external)


class CustomerSerializer(serializers.ModelSerializer):
    masked_phone = serializers.CharField(read_only=True)
    leads_count = serializers.IntegerField(source="leads.count", read_only=True)
    purchases_count = serializers.IntegerField(read_only=True)
    total_spent = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    class Meta:
        model = Customer
        fields = "__all__"


class ReservationPaymentSerializer(serializers.ModelSerializer):
    created_by_detail = UserSerializer(source="created_by", read_only=True)

    class Meta:
        model = ReservationPayment
        fields = "__all__"
        read_only_fields = ["created_by"]


class ReservationPaymentRequestSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    method = serializers.ChoiceField(choices=ReservationPayment.METHOD_CHOICES, required=False, default="cash")
    paid_at = serializers.DateTimeField(required=False)
    note = serializers.CharField(required=False, allow_blank=True)


class ReservationSerializer(serializers.ModelSerializer):
    customer_detail = CustomerSerializer(source="customer", read_only=True)
    catalog_detail = serializers.SerializerMethodField()
    payments = ReservationPaymentSerializer(many=True, read_only=True)
    paid_amount = serializers.SerializerMethodField()
    remaining_amount = serializers.SerializerMethodField()
    customer_name = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=160)
    customer_phone = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=30)

    class Meta:
        model = Reservation
        fields = "__all__"
        read_only_fields = ["created_by"]
        extra_kwargs = {"customer": {"required": False}}

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_catalog_detail(self, obj):
        if not obj.catalog_item_id:
            return None
        return {"id": obj.catalog_item_id, "name_uz": obj.catalog_item.name_uz, "price": str(obj.catalog_item.price), "status": obj.catalog_item.status}

    @extend_schema_field(serializers.DecimalField(max_digits=12, decimal_places=2))
    def get_paid_amount(self, obj):
        return sum((row.amount for row in obj.payments.all()), Decimal("0"))

    @extend_schema_field(serializers.DecimalField(max_digits=12, decimal_places=2))
    def get_remaining_amount(self, obj):
        base = Decimal(obj.estimated_price or 0)
        if obj.catalog_item_id:
            base = Decimal(obj.catalog_item.price or 0)
        return max(base - self.get_paid_amount(obj), Decimal("0"))

    def validate(self, attrs):
        customer = attrs.get("customer") or getattr(self.instance, "customer", None)
        if not customer and not attrs.get("customer_name"):
            raise serializers.ValidationError({"customer_name": "Bron uchun mijoz ismi kerak"})
        if not customer and not attrs.get("customer_phone"):
            raise serializers.ValidationError({"customer_phone": "Bron uchun telefon kerak"})
        return attrs

    def create(self, validated_data):
        validated_data["customer"] = resolve_or_create_customer(
            customer=validated_data.pop("customer", None),
            name=validated_data.pop("customer_name", ""),
            phone=validated_data.pop("customer_phone", ""),
        )
        return super().create(validated_data)

    def update(self, instance, validated_data):
        customer_name = validated_data.pop("customer_name", "")
        customer_phone = validated_data.pop("customer_phone", "")
        if "customer" in validated_data or customer_name or customer_phone:
            validated_data["customer"] = resolve_or_create_customer(
                customer=validated_data.pop("customer", None),
                name=customer_name,
                phone=customer_phone,
            )
        return super().update(instance, validated_data)


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = "__all__"


class ConversationSerializer(serializers.ModelSerializer):
    customer_detail = CustomerSerializer(source="customer", read_only=True)
    messages = MessageSerializer(many=True, read_only=True)
    last_message = serializers.SerializerMethodField()
    source = serializers.SerializerMethodField()
    source_label = serializers.SerializerMethodField()
    ai_is_active = serializers.SerializerMethodField()
    class Meta:
        model = Conversation
        fields = "__all__"

    def to_representation(self, instance):
        if instance.ai_paused_until and instance.ai_paused_until <= timezone.now():
            instance.ai_paused_until = None
            instance.ai_pause_reason = ""
            instance.save(update_fields=["ai_paused_until", "ai_pause_reason", "updated_at"])
        return super().to_representation(instance)

    def get_last_message(self, obj) -> dict[str, Any] | None:
        message = obj.messages.last()
        return MessageSerializer(message).data if message else None

    def get_ai_is_active(self, obj) -> bool:
        return obj.status == "ai" and not (obj.ai_paused_until and obj.ai_paused_until > timezone.now())

    def get_source(self, obj) -> str:
        external_id = obj.customer.instagram_user_id if obj.customer_id else ""
        if external_id.startswith("telegram:"):
            return "telegram"
        if external_id.startswith("miniapp:"):
            return "mini_app"
        return "instagram"

    def get_source_label(self, obj) -> str:
        labels = {"telegram": "Telegram", "mini_app": "Mini app", "instagram": "Instagram"}
        return labels[self.get_source(obj)]


class LeadPackagingUsageInputSerializer(serializers.Serializer):
    packaging = serializers.PrimaryKeyRelatedField(queryset=Packaging.objects.all())
    quantity = serializers.IntegerField(min_value=1, default=1)


class LeadCatalogUsageInputSerializer(serializers.Serializer):
    catalog_item = serializers.PrimaryKeyRelatedField(queryset=CatalogItem.objects.all())
    quantity = serializers.IntegerField(min_value=1, default=1)


class LeadMoveSerializer(serializers.Serializer):
    status = serializers.CharField(required=False, max_length=40)
    before = serializers.PrimaryKeyRelatedField(queryset=Lead.objects.all(), required=False, allow_null=True)
    after = serializers.PrimaryKeyRelatedField(queryset=Lead.objects.all(), required=False, allow_null=True)
    sort_order = serializers.DecimalField(max_digits=20, decimal_places=6, required=False)


class LeadColumnReorderSerializer(serializers.Serializer):
    status = serializers.CharField(max_length=40)
    lead_ids = serializers.ListField(child=serializers.IntegerField(min_value=1))


class LeadSerializer(serializers.ModelSerializer):
    customer_detail = CustomerSerializer(source="customer", read_only=True)
    status_detail = serializers.SerializerMethodField()
    stock_usage = serializers.SerializerMethodField()
    packaging_usage = serializers.SerializerMethodField()
    catalog_usage = serializers.SerializerMethodField()
    stock_usage_input = CatalogCompositionSerializer(many=True, write_only=True, required=False)
    packaging_usage_input = LeadPackagingUsageInputSerializer(many=True, write_only=True, required=False)
    catalog_usage_input = LeadCatalogUsageInputSerializer(many=True, write_only=True, required=False)
    customer_name = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=160)
    customer_phone = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=30)
    customer_instagram_user_id = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=100)
    created_at = serializers.DateTimeField(required=False, help_text="Tarixiy ma'lumot uchun. Berilmasa hozirgi vaqt.")

    class Meta:
        model = Lead
        fields = "__all__"
        extra_kwargs = {"customer": {"required": False}}

    @extend_schema_field(serializers.DictField(allow_null=True))
    def get_status_detail(self, obj):
        status = LeadStatus.objects.filter(key=obj.status).first()
        return LeadStatusSerializer(status).data if status else None

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_stock_usage(self, obj):
        return [{
            "id": row.id,
            "stock_batch": row.stock_batch_id,
            "batch_detail": StockBatchSerializer(row.stock_batch).data,
            "quantity_stems": row.quantity_stems,
            "quantity_bunches": row.quantity_bunches,
        } for row in obj.stock_usage.select_related("stock_batch__variant__flower").all()]

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_packaging_usage(self, obj):
        return [{
            "id": row.id,
            "packaging": row.packaging_id,
            "packaging_detail": PackagingSerializer(row.packaging).data,
            "quantity": row.quantity,
        } for row in obj.packaging_usage.select_related("packaging").all()]

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_catalog_usage(self, obj):
        return [{
            "id": row.id,
            "catalog_item": row.catalog_item_id,
            "catalog_detail": CatalogItemSerializer(row.catalog_item).data,
            "quantity": row.quantity,
        } for row in obj.catalog_usage.select_related("catalog_item__social_post").prefetch_related("catalog_item__composition").all()]

    def validate(self, attrs):
        customer = attrs.get("customer") or getattr(self.instance, "customer", None)
        if not customer and not attrs.get("customer_name"):
            raise serializers.ValidationError({"customer_name": "Yangi lead uchun mijoz ismi kerak"})
        if not customer and not attrs.get("customer_phone"):
            raise serializers.ValidationError({"customer_phone": "Yangi lead uchun telefon kerak"})
        status_value = attrs.get("status")
        if status_value and not LeadStatus.objects.filter(key=status_value).exists():
            raise serializers.ValidationError({"status": "Bunday lead statusi mavjud emas"})
        delivery_at = attrs.get("delivery_at")
        if "delivery_at" in attrs and delivery_at and "recall_at" not in attrs:
            attrs["recall_at"] = delivery_at - timedelta(hours=1)
        return attrs

    def _customer_from_attrs(self, attrs):
        return resolve_or_create_customer(
            customer=attrs.pop("customer", None),
            name=attrs.pop("customer_name", ""),
            phone=attrs.pop("customer_phone", ""),
            external_id=attrs.pop("customer_instagram_user_id", ""),
        )

    def _save_usage(self, lead, stock_rows=None, packaging_rows=None, catalog_rows=None):
        if stock_rows is not None:
            lead.stock_usage.all().delete()
            LeadStockUsage.objects.bulk_create([LeadStockUsage(lead=lead, stock_batch=row["stock_batch"], quantity_stems=row["quantity_stems"], quantity_bunches=row.get("quantity_bunches") or 0) for row in stock_rows])
        if packaging_rows is not None:
            lead.packaging_usage.all().delete()
            LeadPackagingUsage.objects.bulk_create([LeadPackagingUsage(lead=lead, packaging=row["packaging"], quantity=row.get("quantity", 1)) for row in packaging_rows])
        if catalog_rows is not None:
            lead.catalog_usage.all().delete()
            LeadCatalogUsage.objects.bulk_create([LeadCatalogUsage(lead=lead, catalog_item=row["catalog_item"], quantity=row.get("quantity", 1)) for row in catalog_rows])

    def create(self, validated_data):
        stock_rows = validated_data.pop("stock_usage_input", None)
        packaging_rows = validated_data.pop("packaging_usage_input", None)
        catalog_rows = validated_data.pop("catalog_usage_input", None)
        created_at = validated_data.pop("created_at", None)
        customer = self._customer_from_attrs(validated_data)
        lead = Lead.objects.create(customer=customer, **validated_data)
        backdate_record(lead, created_at)
        self._save_usage(lead, stock_rows, packaging_rows, catalog_rows)
        return lead

    def update(self, instance, validated_data):
        stock_rows = validated_data.pop("stock_usage_input", None)
        packaging_rows = validated_data.pop("packaging_usage_input", None)
        catalog_rows = validated_data.pop("catalog_usage_input", None)
        validated_data.pop("customer_name", None)
        validated_data.pop("customer_phone", None)
        validated_data.pop("customer_instagram_user_id", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        instance.save()
        self._save_usage(instance, stock_rows, packaging_rows, catalog_rows)
        return instance


class NotificationSerializer(serializers.ModelSerializer):
    target_user_detail = UserSerializer(source="target_user", read_only=True)

    class Meta:
        model = Notification
        fields = "__all__"


class LeadStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeadStatus
        fields = "__all__"
        read_only_fields = ["created_at", "updated_at"]


class AuditLogSerializer(serializers.ModelSerializer):
    user_detail = UserSerializer(source="user", read_only=True)
    actor_name = serializers.SerializerMethodField()
    action_label = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = "__all__"

    @extend_schema_field(serializers.CharField())
    def get_actor_name(self, obj):
        if not obj.user_id:
            return "System"
        return obj.user.get_full_name() or obj.user.username

    @extend_schema_field(serializers.CharField())
    def get_action_label(self, obj):
        labels = {
            "attendance_check_in": "Ishga keldi",
            "attendance_check_out": "Ishdan ketdi",
            "apprentice_daily_salary_recorded": "Shogird kunlik ish haqi yozildi",
            "catalog_archived": "Katalog arxivlandi",
            "catalog_deleted": "Katalog o‘chirildi",
            "catalog_inventory_deducted": "Katalog uchun sklad kamaytirildi",
            "catalog_inventory_restored": "Katalog qoldig‘i qaytarildi",
            "catalog_sold": "Katalog sotildi",
            "customer_archived": "Mijoz arxivlandi",
            "flower_archived": "Gul turi arxivlandi",
            "flower_deleted": "Gul turi o‘chirildi",
            "florist_salary_created": "Florist ish haqi qo‘shildi",
            "florist_salary_updated": "Florist ish haqi o‘zgartirildi",
            "flowervariant_archived": "Gul navi arxivlandi",
            "flowervariant_deleted": "Gul navi o‘chirildi",
            "lead_created": "Lead yaratildi",
            "lead_deleted": "Lead o‘chirildi",
            "lead_moved": "Lead statusi o‘zgartirildi",
            "lead_reordered": "Lead tartibi o‘zgartirildi",
            "lead_stock_deducted": "Lead uchun sklad kamaytirildi",
            "lead_stock_restored": "Lead sklad qoldig‘i qaytarildi",
            "lead_updated": "Lead tahrirlandi",
            "packaging_adjusted": "Material qoldig‘i o‘zgartirildi",
            "packaging_movement": "Material harakati",
            "packaging_received": "Material kirim qilindi",
            "pagepermission_created": "Permission yaratildi",
            "pagepermission_deleted": "Permission o‘chirildi",
            "pagepermission_updated": "Permission o‘zgartirildi",
            "password_changed": "Password o‘zgartirildi",
            "stock_movement": "Sklad harakati",
            "stock_received": "Sklad kirim qilindi",
            "user_created": "User yaratildi",
            "user_deactivated": "User deaktiv qilindi",
            "user_updated": "User tahrirlandi",
        }
        return labels.get(obj.action, obj.action.replace("_", " ").title())


class BusinessSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessSettings
        fields = "__all__"


class AISettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = AISettings
        fields = "__all__"


class IntegrationSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = IntegrationSettings
        fields = "__all__"


class InstagramSettingsSerializer(serializers.ModelSerializer):
    connected = serializers.SerializerMethodField()
    account_id = serializers.SerializerMethodField()
    has_access_token = serializers.SerializerMethodField()

    class Meta:
        model = InstagramSettings
        fields = ["id", "connected", "account_id", "account_username", "has_access_token", "token_expires_at", "auto_reply_dm", "auto_reply_post_reply", "auto_reply_story_reply", "created_at", "updated_at"]

    def get_connected(self, obj) -> bool:
        return bool(self.context.get("instagram_access_token") and self.context.get("instagram_account_id"))

    def get_account_id(self, obj) -> str:
        return self.context.get("instagram_account_id", "")

    def get_has_access_token(self, obj) -> bool:
        return bool(self.context.get("instagram_access_token"))


class InstagramWebhookEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = InstagramWebhookEvent
        fields = "__all__"


class UploadSerializer(serializers.Serializer):
    file = serializers.FileField()


class UploadResponseSerializer(serializers.Serializer):
    url = serializers.URLField()
    path = serializers.CharField()


class TextRequestSerializer(serializers.Serializer):
    text = serializers.CharField()


class AIPauseRequestSerializer(serializers.Serializer):
    minutes = serializers.IntegerField(required=False, min_value=1)
    paused_until = serializers.DateTimeField(required=False)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, attrs):
        if not attrs.get("minutes") and not attrs.get("paused_until"):
            raise serializers.ValidationError({"detail": "minutes yoki paused_until yuboring"})
        return attrs


class SendResponseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    text = serializers.CharField()
    delivery_status = serializers.ChoiceField(choices=["sent", "failed"], required=False)
    platform_status = serializers.IntegerField(required=False, allow_null=True)
    platform_response = serializers.CharField(required=False, allow_blank=True)


class DebtSerializer(serializers.ModelSerializer):
    customer_detail = serializers.SerializerMethodField(read_only=True)
    catalog_detail = serializers.SerializerMethodField(read_only=True)
    created_by_detail = UserSerializer(source="created_by", read_only=True)
    paid_by_detail = UserSerializer(source="paid_by", read_only=True)
    paid_method_label = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Debt
        fields = "__all__"
        read_only_fields = ["created_by", "paid_by", "paid_at", "is_paid", "paid_method"]

    @extend_schema_field(serializers.DictField())
    def get_customer_detail(self, obj):
        return {"id": obj.customer_id, "name": obj.customer.name, "phone": obj.customer.phone}

    @extend_schema_field(serializers.CharField())
    def get_paid_method_label(self, obj):
        return obj.get_paid_method_display() if obj.paid_method else ""

    @extend_schema_field(serializers.DictField())
    def get_catalog_detail(self, obj):
        """Qarzdorlar sahifasida rasm, gul soni va nomi ko'rinishi uchun."""
        item = obj.catalog_item
        if not item:
            snapshot = (obj.catalog_history.snapshot or {}) if obj.catalog_history_id else {}
            return {"id": None, "name_uz": snapshot.get("catalog", ""), "image_url": "", "stems_per_item": 0, "stems_total": 0}
        stems = sum(row.quantity_stems for row in item.composition.all())
        return {
            "id": item.id,
            "name_uz": item.name_uz,
            "image_url": item.image_url,
            "arrangement_type": item.arrangement_type,
            "volume": item.volume,
            "catalog_kind": item.catalog_kind,
            "stems_per_item": stems,
            "stems_total": stems * int(obj.quantity or 1),
        }


class ExpenseSerializer(serializers.ModelSerializer):
    """Rasxod qatori. Sana yuborilmasa hozirgi vaqt qo'yiladi."""

    payment_method_label = serializers.SerializerMethodField(read_only=True)
    branch_name = serializers.SerializerMethodField(read_only=True)
    created_by_detail = UserSerializer(source="created_by", read_only=True)

    class Meta:
        model = Expense
        fields = "__all__"
        read_only_fields = ["created_by"]
        extra_kwargs = {
            "spent_at": {"required": False, "help_text": "Berilmasa kiritilgan payt olinadi."},
            "destination": {"help_text": "Pul qayerga ketdi."},
        }

    @extend_schema_field(serializers.CharField())
    def get_payment_method_label(self, obj):
        return obj.get_payment_method_display()

    @extend_schema_field(serializers.CharField())
    def get_branch_name(self, obj):
        return obj.branch.name if obj.branch_id else "Asosiy"

    def validate_destination(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Pul qayerga ketganini yozing")
        return value

    def validate_amount(self, value):
        if value is None or Decimal(value) <= 0:
            raise serializers.ValidationError("Summa noldan katta bo‘lishi kerak")
        return value


class StockBatchVariantChangeSerializer(serializers.Serializer):
    """Ishlatilgan partiyada gulni almashtirish.

    Kirimda nav so'ralmaydi, shuning uchun `flower` yuboriladi. `variant`
    eski partiyalarni to'g'rilash uchun qolgan.
    """

    flower = serializers.PrimaryKeyRelatedField(queryset=Flower.objects.all(), required=False, help_text="Yangi gul.")
    variant = serializers.PrimaryKeyRelatedField(queryset=FlowerVariant.objects.all(), required=False, help_text="Eski usul — aniq nav.")
    reason = serializers.CharField(help_text="Nega almashtirilayotgani — audit jurnaliga yoziladi.")

    def validate(self, attrs):
        if not attrs.get("flower") and not attrs.get("variant"):
            raise serializers.ValidationError({"flower": "Gulni tanlang"})
        if attrs.get("flower") and not attrs.get("variant"):
            attrs["variant"] = general_variant_for(attrs["flower"])
        return attrs


class DebtPayRequestSerializer(serializers.Serializer):
    """Qarzni to'landi deb belgilash."""

    method = serializers.ChoiceField(choices=["cash", "card"])
    paid_at = serializers.DateTimeField(required=False, help_text="Berilmasa hozirgi vaqt.")


class CatalogSaleRowSerializer(serializers.ModelSerializer):
    """Katalog sahifasidagi sotuv tarixi qatori."""

    catalog_item = serializers.IntegerField(source="catalog_item_id", read_only=True)
    catalog_name = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    arrangement_type = serializers.SerializerMethodField()
    volume = serializers.SerializerMethodField()
    volume_label = serializers.SerializerMethodField()
    catalog_kind = serializers.SerializerMethodField()
    branch_name = serializers.SerializerMethodField()
    florist_name = serializers.SerializerMethodField()
    payment_type = serializers.SerializerMethodField()
    payment_label = serializers.SerializerMethodField()
    payment_breakdown = serializers.SerializerMethodField()
    sale_total = serializers.SerializerMethodField()
    listed_total = serializers.SerializerMethodField()
    sale_image_url = serializers.SerializerMethodField()
    delivery_amount = serializers.SerializerMethodField()
    received_total = serializers.SerializerMethodField()
    sold_by = serializers.SerializerMethodField()

    class Meta:
        model = CatalogHistory
        fields = [
            "id", "catalog_item", "catalog_name", "image_url", "arrangement_type",
            "volume", "volume_label", "catalog_kind", "branch_name", "florist_name",
            "quantity", "listed_unit_price", "sold_unit_price", "listed_total", "sale_total",
            "discount_amount", "discount_percent", "discount_reason",
            "payment_type", "payment_label", "payment_breakdown",
            "delivery_amount", "received_total", "sale_image_url", "sold_by", "created_at",
        ]

    @extend_schema_field(serializers.CharField())
    def get_catalog_name(self, obj):
        return obj.catalog_item.name_uz if obj.catalog_item_id else (obj.snapshot or {}).get("catalog", "")

    @extend_schema_field(serializers.CharField())
    def get_image_url(self, obj):
        return obj.catalog_item.image_url if obj.catalog_item_id else ""

    @extend_schema_field(serializers.CharField())
    def get_arrangement_type(self, obj):
        return obj.catalog_item.arrangement_type if obj.catalog_item_id else ""

    @extend_schema_field(serializers.CharField())
    def get_volume(self, obj):
        return obj.catalog_item.volume if obj.catalog_item_id else ""

    @extend_schema_field(serializers.CharField())
    def get_volume_label(self, obj):
        from .views import volume_text

        return volume_text(obj.catalog_item.volume) if obj.catalog_item_id else ""

    @extend_schema_field(serializers.CharField())
    def get_catalog_kind(self, obj):
        return obj.catalog_item.catalog_kind if obj.catalog_item_id else ""

    @extend_schema_field(serializers.CharField())
    def get_branch_name(self, obj):
        item = obj.catalog_item
        if not item:
            return ""
        return item.branch.name if item.branch_id else "Toshkent (asosiy filial)"

    @extend_schema_field(serializers.CharField())
    def get_florist_name(self, obj):
        item = obj.catalog_item
        return str(item.florist) if item and item.florist_id else ""

    @extend_schema_field(serializers.CharField())
    def get_payment_type(self, obj):
        from .views import catalog_payment_type

        value = (catalog_payment_type(obj.catalog_item, obj) or "").lower()
        if value in ["naqd", "cash"]:
            return "cash"
        if value in ["karta", "card"]:
            return "card"
        if value in ["terminal", "debt", "mixed"]:
            return value
        return "unknown"

    @extend_schema_field(serializers.CharField())
    def get_payment_label(self, obj):
        return {"cash": "Naqd", "card": "Karta", "terminal": "Terminal", "debt": "Qarz", "mixed": "Aralash"}.get(
            self.get_payment_type(obj), "Aniqlanmagan")

    @extend_schema_field(serializers.DictField())
    def get_payment_breakdown(self, obj):
        """Aralash to'lovda qanchasi naqd, qanchasi karta."""
        if self.get_payment_type(obj) != "mixed":
            return None
        snapshot = obj.snapshot or {}
        return {
            "cash": Decimal(str(snapshot.get("payment_cash") or 0)),
            "card": Decimal(str(snapshot.get("payment_card") or 0)),
            "terminal": Decimal(str(snapshot.get("payment_terminal") or 0)),
        }

    @extend_schema_field(serializers.DecimalField(max_digits=14, decimal_places=2))
    def get_sale_total(self, obj):
        """Tovar savdosi — dastafka ayirilgan holda.

        Sotuvda kiritilgan narx mijoz to'lagan to'liq pul, dastafka esa
        uning ichida bo'ladi va kuryerga ketadi.
        """
        total = self.get_received_total(obj) - self.get_delivery_amount(obj)
        return total if total > 0 else Decimal("0")

    @extend_schema_field(serializers.DecimalField(max_digits=14, decimal_places=2))
    def get_listed_total(self, obj):
        return Decimal(obj.listed_unit_price or 0) * Decimal(obj.quantity or 0)

    @extend_schema_field(serializers.DecimalField(max_digits=12, decimal_places=2))
    def get_delivery_amount(self, obj):
        return Decimal(str((obj.snapshot or {}).get("delivery_amount") or 0))

    @extend_schema_field(serializers.DecimalField(max_digits=14, decimal_places=2))
    def get_received_total(self, obj):
        """Mijozdan olingan umumiy summa — sotuvda kiritilgan narxning o'zi."""
        return Decimal(obj.sold_unit_price or 0) * Decimal(obj.quantity or 0)

    @extend_schema_field(serializers.CharField())
    def get_sale_image_url(self, obj):
        return (obj.snapshot or {}).get("sale_image_url", "")

    @extend_schema_field(serializers.CharField())
    def get_sold_by(self, obj):
        user = obj.created_by
        return (user.get_full_name() or user.username) if user else ""


class CatalogWasteRequestSerializer(serializers.Serializer):
    """Sotilmay qolgan katalogni chiqitga chiqarish."""

    quantity = serializers.IntegerField(min_value=1, required=False, default=1)
    reason = serializers.CharField(required=False, allow_blank=True, help_text="Nega chiqitga chiqdi.")


class CatalogSellRequestSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1, required=False, default=1)
    sale_price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    discount_reason = serializers.CharField(required=False, allow_blank=True)
    payment_type = serializers.ChoiceField(choices=["cash", "card", "terminal", "debt", "mixed"], required=False)
    # Aralash to'lov: summa naqd, karta va terminal orasida bo'linadi.
    # 200 000 so'mlik gulning 100 000 i terminal, 100 000 i naqd bo'lishi mumkin.
    cash_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=Decimal("0"))
    card_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=Decimal("0"))
    terminal_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=Decimal("0"))
    # Dastafka sotuv summasining ichida bo'ladi: kuryerga ketadi, savdodan ayriladi.
    delivery_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, min_value=Decimal("0"),
        help_text="Sotuv summasining ichidagi yetkazib berish puli. Kuryerga beriladi, "
                  "savdodan ayriladi — sotuv narxining ustiga qo‘shilmaydi.",
    )
    sold_at = serializers.DateTimeField(required=False, help_text="Tarixiy sotuv uchun. Berilmasa hozirgi vaqt.")
    reservation = serializers.PrimaryKeyRelatedField(queryset=Reservation.objects.all(), required=False, allow_null=True)
    materials = CatalogMaterialUsageSerializer(many=True, required=False)
    decoration_florist = serializers.PrimaryKeyRelatedField(queryset=FloristProfile.objects.all(), required=False, allow_null=True)
    # Qarzga sotilganda mijoz majburiy: bor mijoz tanlanadi yoki ism-raqamdan yangisi ochiladi
    customer = serializers.PrimaryKeyRelatedField(queryset=Customer.objects.all(), required=False, allow_null=True)
    customer_name = serializers.CharField(required=False, allow_blank=True, max_length=160)
    customer_phone = serializers.CharField(required=False, allow_blank=True, max_length=30)
    debt_note = serializers.CharField(required=False, allow_blank=True)
    # Sotilgan mahsulot rasmi — berilsa guruhga yuboriladi. Hozircha ixtiyoriy.
    sale_image = serializers.FileField(required=False, write_only=True)
    sale_image_url = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if "materials" in attrs:
            attrs["materials"] = normalize_catalog_material_rows(attrs["materials"])
        if attrs.get("payment_type") == "mixed":
            parts = {key: Decimal(str(attrs.get(f"{key}_amount") or 0))
                     for key in ["cash", "card", "terminal"]}
            given = [key for key, amount in parts.items() if amount > 0]
            if len(given) < 2:
                raise serializers.ValidationError({
                    "cash_amount": "Aralash to‘lovda kamida ikkita summani kiriting: "
                                   "naqd, karta yoki terminal",
                })
            for key in ["cash", "card", "terminal"]:
                attrs[f"{key}_amount"] = parts[key]
        if attrs.get("payment_type") == "debt":
            has_customer = attrs.get("customer") is not None
            has_contact = bool((attrs.get("customer_name") or "").strip()) and bool((attrs.get("customer_phone") or "").strip())
            if not has_customer and not has_contact:
                raise serializers.ValidationError({
                    "customer": "Qarzga sotishda mijozni tanlang yoki ism bilan telefon raqamini kiriting",
                })
        return attrs


class DeliveryLocationSerializer(serializers.Serializer):
    """Mijoz xaritada belgilagan manzil. Frontend ochiq sahifadan yuboradi."""

    lead_id = serializers.IntegerField(min_value=1)
    token = serializers.CharField(max_length=64)
    latitude = serializers.DecimalField(max_digits=16, decimal_places=10, min_value=Decimal("-90"), max_value=Decimal("90"))
    longitude = serializers.DecimalField(max_digits=16, decimal_places=10, min_value=Decimal("-180"), max_value=Decimal("180"))
    address = serializers.CharField(required=False, allow_blank=True, max_length=255)


class CatalogSaleRestoreRequestSerializer(serializers.Serializer):
    sale_history = serializers.PrimaryKeyRelatedField(queryset=CatalogHistory.objects.filter(action="sold", quantity__gt=0), required=False)
    quantity = serializers.IntegerField(min_value=1, required=False)
    reason = serializers.CharField(required=False, allow_blank=True)


class CatalogCustomReturnRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True)


class CatalogRestoreFlowersSerializer(serializers.Serializer):
    florist = serializers.PrimaryKeyRelatedField(queryset=FloristProfile.objects.all())
    old_batch = serializers.PrimaryKeyRelatedField(queryset=StockBatch.objects.all())
    new_batch = serializers.PrimaryKeyRelatedField(queryset=StockBatch.objects.all())
    quantity_stems = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(required=False, allow_blank=True)


class CatalogReworkSourceInputSerializer(serializers.Serializer):
    """Buziladigan katalog mahsuloti."""

    catalog_item = serializers.PrimaryKeyRelatedField(queryset=CatalogItem.objects.all())
    quantity = serializers.IntegerField(min_value=1, default=1)


class CatalogReworkStockInputSerializer(serializers.Serializer):
    """Restavratsiya uchun skladdan qo'shimcha olinadigan gul."""

    stock_batch = serializers.PrimaryKeyRelatedField(queryset=StockBatch.objects.all())
    quantity_stems = serializers.IntegerField(min_value=1)


class CatalogReworkCompositionSerializer(serializers.Serializer):
    """Yangi mahsulotning bir donasidagi gul."""

    stock_batch = serializers.PrimaryKeyRelatedField(queryset=StockBatch.objects.all())
    quantity_stems = serializers.IntegerField(min_value=1)


class CatalogReworkMaterialSerializer(serializers.Serializer):
    """Yangi mahsulotning bir donasidagi qadoq."""

    packaging = serializers.PrimaryKeyRelatedField(queryset=Packaging.objects.all())
    quantity = serializers.IntegerField(min_value=1, default=1)


class CatalogReworkOutputInputSerializer(serializers.Serializer):
    """Restavratsiyadan chiqadigan yangi katalog mahsuloti."""

    name_uz = serializers.CharField(max_length=180)
    description_uz = serializers.CharField(required=False, allow_blank=True, default="")
    note = serializers.CharField(required=False, allow_blank=True, default="")
    arrangement_type = serializers.ChoiceField(choices=["bouquet", "basket", "box"], default="bouquet")
    catalog_kind = serializers.ChoiceField(choices=["standard", "custom"], required=False, default="standard")
    volume = serializers.CharField(required=False, allow_blank=True, default="")
    branch = serializers.PrimaryKeyRelatedField(queryset=Branch.objects.all(), required=False, allow_null=True)
    height_cm = serializers.IntegerField(required=False, allow_null=True)
    diameter_cm = serializers.IntegerField(required=False, allow_null=True)
    price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    quantity = serializers.IntegerField(min_value=1, default=1)
    status = serializers.ChoiceField(choices=["draft", "available"], required=False, default="available")
    image_url = serializers.CharField(required=False, allow_blank=True, default="")
    composition = CatalogReworkCompositionSerializer(many=True)
    materials = CatalogReworkMaterialSerializer(many=True, required=False, default=list)


class CatalogReworkCreateSerializer(serializers.Serializer):
    """Restavratsiya hujjatini yaratish so'rovi."""

    florist = serializers.PrimaryKeyRelatedField(queryset=FloristProfile.objects.all())
    florist_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)
    note = serializers.CharField(required=False, allow_blank=True, default="")
    sources = CatalogReworkSourceInputSerializer(many=True, required=False, default=list)
    stock_inputs = CatalogReworkStockInputSerializer(many=True, required=False, default=list)
    outputs = CatalogReworkOutputInputSerializer(many=True)

    def validate(self, attrs):
        if not attrs.get("sources") and not attrs.get("stock_inputs"):
            raise serializers.ValidationError("Kamida bitta buziladigan katalog yoki skladdan gul tanlang")
        if not attrs.get("outputs"):
            raise serializers.ValidationError("Kamida bitta yangi mahsulot kiritilishi kerak")
        return attrs


class CatalogReworkSourceSerializer(serializers.ModelSerializer):
    catalog_item_name = serializers.CharField(source="catalog_item.name_uz", read_only=True)

    class Meta:
        model = CatalogReworkSource
        fields = ["id", "catalog_item", "catalog_item_name", "quantity", "stems", "unit_cost", "cost"]


class CatalogReworkStockInputReadSerializer(serializers.ModelSerializer):
    batch_number = serializers.CharField(source="stock_batch.batch_number", read_only=True)
    variant_name = serializers.SerializerMethodField()

    class Meta:
        model = CatalogReworkStockInput
        fields = ["id", "stock_batch", "batch_number", "variant_name", "quantity_stems", "cost"]

    def get_variant_name(self, obj):
        variant = obj.stock_batch.variant
        parts = [variant.flower.name_uz, variant.name_uz, variant.color_uz]
        return " ".join(part for part in parts if part).strip()


class CatalogReworkOutputSerializer(serializers.ModelSerializer):
    catalog_item_name = serializers.CharField(source="catalog_item.name_uz", read_only=True)
    catalog_item_price = serializers.DecimalField(source="catalog_item.price", max_digits=12, decimal_places=2, read_only=True)
    image_url = serializers.CharField(source="catalog_item.image_url", read_only=True)

    class Meta:
        model = CatalogReworkOutput
        fields = [
            "id", "catalog_item", "catalog_item_name", "catalog_item_price", "image_url",
            "quantity", "stems", "allocated_cost", "allocated_florist_amount",
        ]


class CatalogReworkSerializer(serializers.ModelSerializer):
    florist_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    sources = CatalogReworkSourceSerializer(many=True, read_only=True)
    stock_inputs = CatalogReworkStockInputReadSerializer(many=True, read_only=True)
    outputs = CatalogReworkOutputSerializer(many=True, read_only=True)

    class Meta:
        model = CatalogRework
        fields = [
            "id", "florist", "florist_name", "florist_amount",
            "input_stems", "output_stems", "waste_stems",
            "input_cost", "waste_cost", "note",
            "created_by", "created_by_name", "created_at", "updated_at",
            "sources", "stock_inputs", "outputs",
        ]
        read_only_fields = fields

    def get_florist_name(self, obj):
        return str(obj.florist) if obj.florist_id else ""

    def get_created_by_name(self, obj):
        if not obj.created_by_id:
            return ""
        return obj.created_by.get_full_name() or obj.created_by.username


class SimulateResponseSerializer(serializers.Serializer):
    reply = serializers.CharField(allow_null=True)


class MovementRequestSerializer(serializers.Serializer):
    movement_type = serializers.ChoiceField(choices=StockMovement.TYPE_CHOICES)
    quantity_stems = serializers.IntegerField(min_value=1, required=False)
    quantity_bunches = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)
    reason = serializers.CharField(required=False, allow_blank=True)
    created_at = serializers.DateTimeField(required=False, help_text="Tarixiy ma'lumot uchun. Berilmasa hozirgi vaqt.")

    def validate(self, attrs):
        if not attrs.get("quantity_stems") and not attrs.get("quantity_bunches"):
            raise serializers.ValidationError({"quantity": "quantity_stems yoki quantity_bunches yuboring"})
        return attrs


class StockBatchSellRequestSerializer(serializers.Serializer):
    quantity_stems = serializers.IntegerField(min_value=1)
    sale_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"))
    payment_type = serializers.ChoiceField(choices=["cash", "card", "debt", "mixed"], required=False, allow_blank=True)
    cash_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=Decimal("0"))
    card_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=Decimal("0"))
    reason = serializers.CharField(required=False, allow_blank=True)
    sold_at = serializers.DateTimeField(required=False)

    def validate(self, attrs):
        payment_type = attrs.get("payment_type") or ""
        sale_amount = Decimal(attrs.get("sale_amount") or 0).quantize(Decimal("0.01"))
        if payment_type != "mixed":
            attrs.pop("cash_amount", None)
            attrs.pop("card_amount", None)
            return attrs
        cash = attrs.get("cash_amount")
        card = attrs.get("card_amount")
        if cash is None or card is None:
            raise serializers.ValidationError({
                "cash_amount": "Aralash to‘lovda naqd va karta summasini kiriting",
                "card_amount": "Aralash to‘lovda naqd va karta summasini kiriting",
            })
        if cash <= 0 or card <= 0:
            raise serializers.ValidationError({"cash_amount": "Aralash to‘lovda ikkala summa ham noldan katta bo‘lishi kerak"})
        given = (Decimal(cash) + Decimal(card)).quantize(Decimal("0.01"))
        if given != sale_amount:
            raise serializers.ValidationError({"detail": f"Naqd va karta yig‘indisi sotuv summasiga teng emas. Sotuv: {sale_amount}, kiritilgan: {given}"})
        return attrs


class PackagingMovementRequestSerializer(serializers.Serializer):
    movement_type = serializers.ChoiceField(choices=PackagingMovement.TYPE_CHOICES)
    quantity = serializers.IntegerField()
    reason = serializers.CharField(required=False, allow_blank=True)
    created_at = serializers.DateTimeField(required=False, help_text="Tarixiy ma'lumot uchun. Berilmasa hozirgi vaqt.")

    def validate(self, attrs):
        if attrs["movement_type"] == "adjustment":
            if attrs["quantity"] == 0:
                raise serializers.ValidationError({"quantity": "0 bo‘lmasligi kerak"})
        elif attrs["quantity"] < 1:
            raise serializers.ValidationError({"quantity": "Musbat son kiriting"})
        return attrs


class PackagingSellRequestSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1, required=False, default=1)
    sale_price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=Decimal("0"))
    payment_type = serializers.ChoiceField(choices=["cash", "card", "debt", "mixed"], required=False, allow_blank=True)
    cash_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=Decimal("0"))
    card_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=Decimal("0"))
    reason = serializers.CharField(required=False, allow_blank=True)
    sold_at = serializers.DateTimeField(required=False)

    def validate(self, attrs):
        if attrs.get("payment_type") != "mixed":
            attrs.pop("cash_amount", None)
            attrs.pop("card_amount", None)
            return attrs
        cash = attrs.get("cash_amount")
        card = attrs.get("card_amount")
        if cash is None or card is None:
            raise serializers.ValidationError({
                "cash_amount": "Aralash to‘lovda naqd va karta summasini kiriting",
                "card_amount": "Aralash to‘lovda naqd va karta summasini kiriting",
            })
        if cash <= 0 or card <= 0:
            raise serializers.ValidationError({"cash_amount": "Aralash to‘lovda ikkala summa ham noldan katta bo‘lishi kerak"})
        quantity = int(attrs.get("quantity") or 1)
        unit_price = attrs.get("sale_price")
        if unit_price is not None:
            expected = (Decimal(unit_price) * Decimal(quantity)).quantize(Decimal("0.01"))
            given = (Decimal(cash) + Decimal(card)).quantize(Decimal("0.01"))
            if given != expected:
                raise serializers.ValidationError({"detail": f"Naqd va karta yig‘indisi sotuv summasiga teng emas. Sotuv: {expected}, kiritilgan: {given}"})
        return attrs


class MiniAppInitSerializer(serializers.Serializer):
    init_data = serializers.CharField(required=False, allow_blank=True)


class MiniAppLineSerializer(serializers.Serializer):
    stock_batch = serializers.IntegerField(required=False)
    catalog_item = serializers.IntegerField(required=False)
    quantity_stems = serializers.IntegerField(required=False, min_value=1)
    quantity = serializers.IntegerField(required=False, min_value=1)


class MiniAppQuoteSerializer(serializers.Serializer):
    init_data = serializers.CharField(required=False, allow_blank=True)
    arrangement_type = serializers.ChoiceField(choices=["bouquet", "basket", "catalog"])
    items = MiniAppLineSerializer(many=True, required=False)
    packaging = serializers.IntegerField(required=False, allow_null=True)
    request_text = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        arrangement_type = attrs.get("arrangement_type")
        items = attrs.get("items") or []
        request_text = (attrs.get("request_text") or "").strip()
        if arrangement_type == "catalog":
            if not items:
                raise serializers.ValidationError({"items": "Katalog buyurtmasi uchun catalog_item yuboring"})
            if any(not row.get("catalog_item") for row in items):
                raise serializers.ValidationError({"items": "Mini app katalogda faqat catalog_item ishlatiladi"})
        elif not request_text:
            raise serializers.ValidationError({"request_text": "Yasatish uchun mijoz yozgan matn kerak"})
        attrs["items"] = items
        attrs["request_text"] = request_text
        return attrs


class MiniAppLeadSerializer(MiniAppQuoteSerializer):
    name = serializers.CharField(max_length=160)
    phone = serializers.CharField(max_length=30)
    note = serializers.CharField(required=False, allow_blank=True)


class EuroFlowersTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        data["permissions"] = permission_matrix(self.user)
        return data
