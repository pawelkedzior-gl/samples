import logging
from typing import Any

import httpx
import config
import uuid

from enums import CheckoutStatus
from models import UnifiedCheckout as Checkout
from models import UnifiedCheckoutCreateRequest
from models import UnifiedCheckoutUpdateRequest

from ucp_sdk.models.schemas.shopping.order import PlatformSchema
from ucp_sdk.models.schemas.shopping.payment_create_request import PaymentCreateRequest
from ucp_sdk.models.schemas.shopping.ap2_mandate import Checkout as Ap2CompleteRequest
from ucp_sdk.models.schemas.ucp import ResponseCheckoutSchema as ResponseCheckout
from ucp_sdk.models.schemas.shopping.payment import Payment as PaymentResponse
from ucp_sdk.models.schemas.ucp import ResponseOrderSchema as ResponseOrder
from ucp_sdk.models.schemas.ucp import UcpMetadata
from ucp_sdk.models.schemas.ucp import Version
from ucp_sdk.models.schemas.capability import ResponseSchema as Response
from ucp_sdk.models.schemas.shopping.discount import Allocation
from ucp_sdk.models.schemas.shopping.discount import AppliedDiscount
from ucp_sdk.models.schemas.shopping.discount import DiscountsObject
from ucp_sdk.models.schemas.shopping.fulfillment import (
    Fulfillment as FulfillmentWrapper,
)
from ucp_sdk.models.schemas.shopping.order import (
    Fulfillment as OrderFulfillment,
)
from ucp_sdk.models.schemas.shopping.order import Order
from ucp_sdk.models.schemas.shopping.types import order_line_item
from ucp_sdk.models.schemas.shopping.types import total as total_resp
from ucp_sdk.models.schemas.shopping.types.expectation import Expectation
from ucp_sdk.models.schemas.shopping.types.fulfillment_destination import (
    FulfillmentDestination,
)
from ucp_sdk.models.schemas.shopping.types.fulfillment_group import (
    FulfillmentGroup,
)
from ucp_sdk.models.schemas.shopping.types.fulfillment_method import (
    FulfillmentMethod,
)
from ucp_sdk.models.schemas.shopping.types.fulfillment import (
    Fulfillment as FulfillmentResponseClass,
)
from ucp_sdk.models.schemas.shopping.types.item import Item as ItemResponse
from ucp_sdk.models.schemas.shopping.types.line_item import (
    LineItem as LineItemResponse,
)
from ucp_sdk.models.schemas.shopping.types.order_confirmation import (
    OrderConfirmation,
)
from ucp_sdk.models.schemas.shopping.types.order_line_item import OrderLineItem
from ucp_sdk.models.schemas.shopping.types.postal_address import PostalAddress
from ucp_sdk.models.schemas.shopping.types.shipping_destination import (
    ShippingDestination as ShippingDestinationResponse,
)
from ucp_sdk.models.schemas.shopping.types.total import (
    Total as TotalResponse,
)

logger = logging.getLogger(__name__)


class CheckoutService:
    def __init__(
        self,
        fulfillment_service,
        products_session,
        transactions_session,
        base_url: str,
        woo_commerce_address: str | None = None,
    ):
        logger.info("WooCommerce CheckoutService initialized")
        self.fulfillment_service = fulfillment_service
        self.products_session = products_session
        self.transactions_session = transactions_session
        self.base_url = base_url
        self.woo_commerce_address = woo_commerce_address

    def _log_if_error(self, response: httpx.Response):
        if not response.is_success:
            logger.error(
                "Request failed with status %s: %s", response.status_code, response.text
            )

    async def _get_cart(
        self, client: httpx.AsyncClient, cart_token: str | None
    ) -> tuple[dict[str, Any], str | None, str | None]:
        headers = {}
        if cart_token:
            headers["Cart-Token"] = cart_token

        response = await client.get("wp-json/wc/store/v1/cart", headers=headers)
        self._log_if_error(response)
        logger.info("WooCommerce API Response Headers: %s", response.headers)

        last_cart_resp = response.json()

        nonce = response.headers.get("Nonce")
        nonce_timestamp = response.headers.get("Nonce-Timestamp")
        user_id = response.headers.get("User-ID")
        cart_token = response.headers.get("Cart-Token") or cart_token
        cart_hash = response.headers.get("Cart-Hash")

        logger.info(
            "Extracted WooCommerce headers: nonce=%s, timestamp=%s, user_id=%s, cart_token=%s, cart_hash=%s",
            nonce,
            nonce_timestamp,
            user_id,
            cart_token,
            cart_hash,
        )
        return last_cart_resp, cart_token, nonce

    async def _sync_cart(
        self,
        client: httpx.AsyncClient,
        cart_token: str | None,
        requested_line_items: list[Any],
        requested_discounts: DiscountsObject | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        cart, cart_token, nonce = await self._get_cart(client, cart_token)

        # Update headers for subsequent write requests
        headers = {}
        if cart_token:
            headers["Cart-Token"] = cart_token
        if nonce:
            headers["Nonce"] = nonce

        current_items = cart.get("items", [])
        current_items_by_id = {str(item["id"]): item for item in current_items}
        requested_ids = {str(li.item.id) for li in requested_line_items}

        # Remove items not in request
        for item in current_items:
            item_id = str(item["id"])
            if item_id not in requested_ids:
                logger.info("Removing item from cart: key=%s", item["key"])
                params = {"key": item["key"]}
                resp = await client.post(
                    "wp-json/wc/store/v1/cart/remove-item",
                    headers=headers,
                    params=params,
                )
                self._log_if_error(resp)
                cart = resp.json()

        # Add or Update items
        for li_req in requested_line_items:
            item_id = str(li_req.item.id)
            if item_id in current_items_by_id:
                item = current_items_by_id[item_id]
                if item["quantity"] != li_req.quantity:
                    logger.info(
                        "Updating item in cart: key=%s, quantity=%s",
                        item["key"],
                        li_req.quantity,
                    )
                    params = {"key": item["key"], "quantity": li_req.quantity}
                    resp = await client.post(
                        "wp-json/wc/store/v1/cart/update-item",
                        headers=headers,
                        params=params,
                    )
                    self._log_if_error(resp)
                    cart = resp.json()
            else:
                logger.info(
                    "Adding item to cart: id=%s, quantity=%s",
                    item_id,
                    li_req.quantity,
                )
                params = {"id": item_id, "quantity": li_req.quantity}
                resp = await client.post(
                    "wp-json/wc/store/v1/cart/add-item",
                    headers=headers,
                    params=params,
                )
                self._log_if_error(resp)
                cart = resp.json()

        # Handle discounts if requested
        if requested_discounts and requested_discounts.codes is not None:
            current_coupons = cart.get("coupons", [])
            current_codes = {c["code"] for c in current_coupons}
            target_codes = set(requested_discounts.codes)

            # Remove coupons not in target
            for code in current_codes - target_codes:
                logger.info("Removing coupon from cart: %s", code)
                resp = await client.post(
                    "wp-json/wc/store/v1/cart/remove-coupon",
                    headers=headers,
                    params={"code": code},
                )
                self._log_if_error(resp)
                cart = resp.json()

            # Add coupons not in current
            for code in target_codes - current_codes:
                logger.info("Applying coupon to cart: %s", code)
                resp = await client.post(
                    "wp-json/wc/store/v1/cart/apply-coupon",
                    headers=headers,
                    params={"code": code},
                )
                self._log_if_error(resp)
                cart = resp.json()

        return cart, cart_token

    def _build_checkout_response(
        self,
        checkout_req: Any,
        last_cart_resp: dict[str, Any],
        checkout_id: str,
        platform_config: PlatformSchema | None = None,
    ) -> Checkout:
        checkout_data = checkout_req.model_dump(
            exclude={
                "line_items",
                "payment",
                "ucp",
                "currency",
                "id",
                "status",
                "totals",
                "links",
                "fulfillment",
                "discounts",
            }
        )

        line_items = []
        for item in last_cart_resp.get("items", []):
            line_items.append(
                LineItemResponse(
                    id=item["key"],
                    item=ItemResponse(
                        id=str(item["id"]),
                        title=item["name"],
                        price=int(item["prices"]["price"]),
                    ),
                    quantity=item["quantity"],
                    totals=[
                        TotalResponse(
                            type="subtotal", amount=int(item["totals"]["line_subtotal"])
                        ),
                        TotalResponse(
                            type="total", amount=int(item["totals"]["line_total"])
                        ),
                    ],
                )
            )

        fulfillment_resp = None
        if checkout_req.fulfillment:
            req_fulfillment = checkout_req.fulfillment.root
            resp_methods = []
            all_li_ids = [li.id for li in line_items]

            if req_fulfillment.methods:
                for method_req in req_fulfillment.methods:
                    method_id = getattr(method_req, "id", None) or str(uuid.uuid4())
                    method_li_ids = (
                        getattr(method_req, "line_item_ids", None) or all_li_ids
                    )
                    method_type = getattr(method_req, "type", "shipping")

                    resp_groups = []
                    if method_req.groups:
                        for group_req in method_req.groups:
                            group_id = (
                                getattr(group_req, "id", None)
                                or f"group_{uuid.uuid4()}"
                            )
                            group_li_ids = (
                                getattr(group_req, "line_item_ids", None) or all_li_ids
                            )

                            resp_groups.append(
                                FulfillmentGroup(
                                    id=group_id,
                                    line_item_ids=group_li_ids,
                                    selected_option_id=getattr(
                                        group_req, "selected_option_id", None
                                    ),
                                )
                            )

                    resp_destinations = []
                    if method_req.destinations:
                        for dest_req in method_req.destinations:
                            inner_dest = dest_req.root
                            resp_destinations.append(
                                FulfillmentDestination(
                                    root=ShippingDestinationResponse(
                                        id=getattr(inner_dest, "id", None)
                                        or str(uuid.uuid4()),
                                        address_country=inner_dest.address_country,
                                        postal_code=inner_dest.postal_code,
                                        address_region=inner_dest.address_region,
                                        address_locality=inner_dest.address_locality,
                                        street_address=inner_dest.street_address,
                                    )
                                )
                            )

                    resp_methods.append(
                        FulfillmentMethod(
                            id=method_id,
                            type=method_type,
                            line_item_ids=method_li_ids,
                            groups=resp_groups or None,
                            destinations=resp_destinations or None,
                            selected_destination_id=getattr(
                                method_req, "selected_destination_id", None
                            ),
                        )
                    )

            fulfillment_resp = FulfillmentWrapper(
                root=FulfillmentResponseClass(methods=resp_methods)
            )

        totals = [
            TotalResponse(
                type="subtotal", amount=int(last_cart_resp["totals"]["total_items"])
            ),
            TotalResponse(
                type="total", amount=int(last_cart_resp["totals"]["total_price"])
            ),
        ]
        if int(last_cart_resp["totals"]["total_shipping"]) > 0:
            totals.append(
                TotalResponse(
                    type="fulfillment",
                    amount=int(last_cart_resp["totals"]["total_shipping"]),
                )
            )
        if int(last_cart_resp["totals"]["total_discount"]) > 0:
            totals.append(
                TotalResponse(
                    type="discount",
                    amount=int(last_cart_resp["totals"]["total_discount"]),
                )
            )

        req_discounts = getattr(checkout_req, "discounts", None)
        applied_discounts = []
        for coupon in last_cart_resp.get("coupons", []):
            applied_discounts.append(
                AppliedDiscount(
                    code=coupon["code"],
                    title=coupon["code"],
                    amount=int(coupon["totals"]["total_discount"]),
                    automatic=False,
                )
            )

        discounts_obj = None
        if applied_discounts or (req_discounts and req_discounts.codes):
            discounts_obj = DiscountsObject(
                codes=req_discounts.codes if req_discounts else [],
                applied=applied_discounts if applied_discounts else None,
            )

        checkout = Checkout(
            ucp=ResponseCheckout(
                version=Version(config.get_server_version()),
                capabilities={
                    "dev.ucp.shopping.checkout": [
                        Response(
                            name="dev.ucp.shopping.checkout",
                            version=Version(config.get_server_version()),
                        )
                    ]
                },
                payment_handlers={},
            ),
            id=checkout_id,
            status=CheckoutStatus.IN_PROGRESS,
            currency=checkout_req.currency,
            line_items=line_items,
            totals=totals,
            links=[],
            payment=(
                PaymentResponse(
                    instruments=(
                        checkout_req.payment.instruments
                        if checkout_req.payment
                        else None
                    ),
                )
                if checkout_req.payment
                else None
            ),
            platform=platform_config,
            fulfillment=fulfillment_resp,
            discounts=discounts_obj,
            **checkout_data,
        )
        return checkout

    async def create_checkout(
        self,
        checkout_req: UnifiedCheckoutCreateRequest,
        idempotency_key: str,
        platform_config: PlatformSchema | None = None,
    ) -> Checkout:
        logger.info("WooCommerce create_checkout")
        async with httpx.AsyncClient(base_url=self.woo_commerce_address) as client:
            cart, cart_token = await self._sync_cart(
                client, None, checkout_req.line_items, checkout_req.discounts
            )

        return self._build_checkout_response(
            checkout_req, cart, cart_token, platform_config
        )

    async def get_checkout(self, checkout_id: str) -> Checkout:
        logger.info("WooCommerce get_checkout")
        async with httpx.AsyncClient(base_url=self.woo_commerce_address) as client:
            cart, cart_token, _ = await self._get_cart(client, checkout_id)

        # Create a dummy request to pass to _build_checkout_response
        dummy_req = UnifiedCheckoutUpdateRequest.model_construct(currency="USD")

        return self._build_checkout_response(dummy_req, cart, cart_token)

    async def update_checkout(
        self,
        checkout_id: str,
        checkout_req: UnifiedCheckoutUpdateRequest,
        idempotency_key: str,
        platform_config: PlatformSchema | None = None,
    ) -> Checkout:
        logger.info("WooCommerce update_checkout")
        async with httpx.AsyncClient(base_url=self.woo_commerce_address) as client:
            cart, cart_token = await self._sync_cart(
                client, checkout_id, checkout_req.line_items, checkout_req.discounts
            )

        return self._build_checkout_response(
            checkout_req, cart, cart_token, platform_config
        )

    async def complete_checkout(
        self,
        checkout_id: str,
        payment: PaymentCreateRequest,
        risk_signals: dict[str, Any],
        idempotency_key: str,
        ap2: Ap2CompleteRequest | None = None,
    ) -> Checkout:
        logger.info("WooCommerce complete_checkout (Hello World)")
        return Checkout.model_construct(
            id=checkout_id, status="completed", currency="USD"
        )

    async def cancel_checkout(self, checkout_id: str, idempotency_key: str) -> Checkout:
        logger.info("WooCommerce cancel_checkout (Hello World)")
        return Checkout.model_construct(
            id=checkout_id, status="canceled", currency="USD"
        )

    async def get_order(self, order_id: str) -> dict[str, Any]:
        logger.info("WooCommerce get_order (Hello World)")
        return {"id": order_id, "status": "processing"}

    async def update_order(
        self, order_id: str, order_data: dict[str, Any]
    ) -> dict[str, Any]:
        logger.info("WooCommerce update_order (Hello World)")
        return {"id": order_id, "status": "updated"}

    async def ship_order(self, order_id: str) -> None:
        logger.info("WooCommerce ship_order (Hello World)")
        pass
