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
        await self._recalculate_totals(checkout)
        return checkout

    async def _recalculate_totals(
        self,
        checkout: Checkout,
    ) -> None:
        """Recalculate line item subtotals and checkout totals."""
        grand_total = 0

        for line in checkout.line_items:
            product_id = line.item.id
            # TODO: DB access
            # product = await db.get_product(self.products_session, product_id)
            class MockProduct:
                price = 1000  # 10.00 USD
                title = "Mock Product"
            product = MockProduct()

            # Use authoritative price and title from DB
            line.item.price = product.price
            line.item.title = product.title

            base_amount = product.price * line.quantity
            line.totals = [
                TotalResponse(type="subtotal", amount=base_amount),
                TotalResponse(type="total", amount=base_amount),
            ]
            grand_total += base_amount

        checkout.totals = []
        # Always include subtotal for clarity when other costs might be added
        checkout.totals.append(TotalResponse(type="subtotal", amount=grand_total))

        # Fulfillment Logic
        if checkout.fulfillment and checkout.fulfillment.root.methods:
            # Fetch promotions once for the loop
            # TODO: DB access
            # promotions = await db.get_active_promotions(self.products_session)
            promotions = []

            for method in checkout.fulfillment.root.methods:
                # 1. Identify Destination and Calculate Options
                calculated_options = []
                if method.type == "shipping" and method.selected_destination_id:
                    selected_dest = None
                    if method.destinations:
                        for dest in method.destinations:
                            if dest.root.id == method.selected_destination_id:
                                selected_dest = dest.root
                                break

                    if selected_dest:
                        logger.info(
                            "Calculating options for country: %s (dest_id: %s)",
                            selected_dest.address_country,
                            method.selected_destination_id,
                        )
                        # Log all available destinations for debugging
                        if method.destinations:
                            logger.info(
                                "Available destinations in method %s: %s",
                                method.id,
                                [
                                    f"{d.root.id} ({d.root.address_country})"
                                    for d in method.destinations
                                ],
                            )
                        try:
                            # Map ShippingDestination to PostalAddress for service call
                            # Using strong types from SDK
                            address_obj = PostalAddress(
                                street_address=selected_dest.street_address,
                                address_locality=selected_dest.address_locality,
                                address_region=selected_dest.address_region,
                                postal_code=selected_dest.postal_code,
                                address_country=selected_dest.address_country,
                            )

                            # Get options from service
                            # We calculate based on the items in this method/group
                            # For simplicity, passing method's line_item_ids if available,
                            # else all.
                            all_li_ids = [li.id for li in checkout.line_items]
                            target_li_ids = method.line_item_ids or all_li_ids

                            # Map Line Item IDs to Product IDs for the service
                            target_product_ids = []
                            for li_uuid in target_li_ids:
                                li = next(
                                    (item for item in checkout.line_items if item.id == li_uuid),
                                    None,
                                )
                                if li:
                                    target_product_ids.append(li.item.id)

                            # TODO: DB access or service dependent on DB
                            # calculated_options_resp = (
                            #     await self.fulfillment_service.calculate_options(
                            #         self.transactions_session,
                            #         address_obj,
                            #         promotions=promotions,
                            #         subtotal=grand_total,
                            #         line_item_ids=target_product_ids,
                            #     )
                            # )
                            # calculated_options = calculated_options_resp
                            calculated_options = []
                        except (ValueError, TypeError) as e:
                            logging.error("Failed to calculate options: %s", e)

                # 2. Generate or Update Groups
                if method.selected_destination_id and not method.groups:
                    # Generate new group
                    group = FulfillmentGroup(
                        id=f"group_{uuid.uuid4()}",
                        line_item_ids=method.line_item_ids,
                        options=calculated_options,
                    )
                    method.groups = [group]
                elif method.groups:
                    # Update existing groups with fresh options
                    for group in method.groups:
                        # Refresh options if they changed due to address/item update
                        if calculated_options:
                            group.options = calculated_options

                        # Recalculate Totals based on Group Selection
                        if group.selected_option_id and group.options:
                            selected_opt = next(
                                (o for o in group.options if o.id == group.selected_option_id),
                                None,
                            )
                            if selected_opt:
                                # Avoid double counting if already added.
                                # Multiple groups can have costs.
                                # We assume each group adds to the total.
                                opt_total = next(
                                    (t.amount for t in selected_opt.totals if t.type == "total"),
                                    0,
                                )
                                grand_total += opt_total
                                checkout.totals.append(
                                    TotalResponse(type="fulfillment", amount=opt_total)
                                )

        # Discount Logic
        if not checkout.discounts:
            checkout.discounts = DiscountsObject()

        if checkout.discounts.codes:
            # Batch fetch discounts to avoid N+1 queries
            # TODO: DB access
            # discounts = await db.get_discounts_by_codes(
            #     self.transactions_session, checkout.discounts.codes
            # )
            # discount_map = {d.code: d for d in discounts}
            discount_map = {}

            for code in checkout.discounts.codes:
                discount_obj = discount_map.get(code)
                if discount_obj:
                    discount_amount = 0
                    if discount_obj.type == "percentage":
                        discount_amount = int(grand_total * (discount_obj.value / 100))
                    elif discount_obj.type == "fixed_amount":
                        discount_amount = discount_obj.value

                    if discount_amount > 0:
                        grand_total -= discount_amount
                        if checkout.discounts.applied is None:
                            checkout.discounts.applied = []
                        checkout.discounts.applied.append(
                            AppliedDiscount(
                                code=code,
                                title=discount_obj.description,
                                amount=discount_amount,
                                allocations=[
                                    Allocation(
                                        path="$.totals[?(@.type=='subtotal')]",
                                        amount=discount_amount,
                                    )
                                ],
                            )
                        )
                        checkout.totals.append(
                            TotalResponse(type="discount", amount=discount_amount)
                        )

        checkout.totals.append(TotalResponse(type="total", amount=grand_total))

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
