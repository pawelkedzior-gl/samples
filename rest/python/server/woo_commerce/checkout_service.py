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
        logger.info("WooCommerce CheckoutService initialized (Hello World)")
        self.fulfillment_service = fulfillment_service
        self.products_session = products_session
        self.transactions_session = transactions_session
        self.base_url = base_url
        self.woo_commerce_address = woo_commerce_address

    async def create_checkout(
        self,
        checkout_req: UnifiedCheckoutCreateRequest,
        idempotency_key: str,
        platform_config: PlatformSchema | None = None,
    ) -> Checkout:
        logger.info("WooCommerce create_checkout (Hello World)")
        async with httpx.AsyncClient(base_url=self.woo_commerce_address) as client:
            response = await client.get("wp-json/wc/store/v1/cart")
            print("WooCommerce API Response Headers:", response.headers)

            nonce = response.headers.get("Nonce")
            nonce_timestamp = response.headers.get("Nonce-Timestamp")
            user_id = response.headers.get("User-ID")
            cart_token = response.headers.get("Cart-Token")
            cart_hash = response.headers.get("Cart-Hash")

            logger.info(
                "Extracted WooCommerce headers: nonce=%s, timestamp=%s, user_id=%s, cart_token=%s, cart_hash=%s",
                nonce,
                nonce_timestamp,
                user_id,
                cart_token,
                cart_hash,
            )

        checkout_id = cart_token

        # We exclude fields that the service explicitly manages or overrides to
        # avoid keyword argument conflicts when constructing the response model.
        # By excluding only these 'base' fields, we allow extension fields (like
        # 'buyer' or 'discounts') to pass through dynamically via **checkout_data.
        #
        # * Conflict Prevention: If we didn't exclude currency, id, or payment,
        #   passing them via **checkout_data while also specifying them as keyword
        #   arguments (e.g., currency=checkout_req.currency) would raise a
        #   TypeError: multiple values for keyword argument.
        # * Server Authority: Fields like status, totals, and links might be
        #   present in a client request (even if they shouldn't be), but the server
        #   is the source of truth. We exclude them from the dumped data to ensure
        #   we start with a "clean" calculated state (e.g.,
        #   status=CheckoutStatus.IN_PROGRESS, totals=[]).
        # * Model Transformation: ucp in the request is usually just version
        #   negotiation info, but in the response, it's a complex ResponseCheckout
        #   object with capability metadata. We exclude the request version to
        #   inject the full response object.
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
            }
        )
        # Map line items
        line_items = []
        for li_req in checkout_req.line_items:
            line_items.append(
                LineItemResponse(
                    id=str(uuid.uuid4()),
                    item=ItemResponse(
                        id=li_req.item.id,
                        title=li_req.item.title,
                        price=0,  # Will be set by recalculate_totals
                    ),
                    quantity=li_req.quantity,
                    totals=[],
                )
            )

        # Initialize fulfillment response
        fulfillment_resp = None
        if checkout_req.fulfillment:
            req_fulfillment = checkout_req.fulfillment.root
            resp_methods = []
            all_li_ids = [li.id for li in line_items]

            if req_fulfillment.methods:
                for method_req in req_fulfillment.methods:
                    # Create Method Response
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

            # Convert destinations if present (usually empty on create, but
            # handled for completeness)
            resp_destinations = []
            if method_req.destinations:
                for dest_req in method_req.destinations:
                    # Assuming ShippingDestinationRequest can map to Response
                    # structure or needs conversion. For create, we typically accept
                    # ShippingDestinationRequest inside
                    # FulfillmentMethodCreateRequest. We need to convert it to
                    # FulfillmentDestinationResponse.
                    # The request model structure is complex
                    # (FulfillmentDestinationRequest -> ShippingDestinationRequest)
                    # The response model is FulfillmentDestinationResponse ->
                    # ShippingDestinationResponse

                    # Extract the inner ShippingDestinationRequest
                    inner_dest = dest_req.root

                    resp_destinations.append(
                        FulfillmentDestination(
                            root=ShippingDestinationResponse(
                                id=getattr(inner_dest, "id", None) or str(uuid.uuid4()),
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
            totals=[],
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
            **checkout_data,
        )
        return checkout

    async def get_checkout(self, checkout_id: str) -> Checkout:
        logger.info("WooCommerce get_checkout (Hello World)")
        return Checkout.model_construct(
            id=checkout_id, status="created", currency="USD"
        )

    async def update_checkout(
        self,
        checkout_id: str,
        checkout_req: UnifiedCheckoutUpdateRequest,
        idempotency_key: str,
        platform_config: PlatformSchema | None = None,
    ) -> Checkout:
        logger.info("WooCommerce update_checkout (Hello World)")
        return Checkout.model_construct(
            id=checkout_id, status="updated", currency="USD"
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
