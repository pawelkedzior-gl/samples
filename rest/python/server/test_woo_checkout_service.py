import pytest
import httpx
from unittest.mock import AsyncMock, patch
from woo_commerce.checkout_service import CheckoutService
from ucp_sdk.models.schemas.shopping.payment_create_request import PaymentCreateRequest
from enums import CheckoutStatus

@pytest.mark.anyio
async def test_complete_checkout_success():
    # Mock fulfillment_service, products_session, transactions_session
    fulfillment_service = AsyncMock()
    products_session = AsyncMock()
    transactions_session = AsyncMock()
    
    service = CheckoutService(
        fulfillment_service,
        products_session,
        transactions_session,
        base_url="http://localhost",
        woo_commerce_address="http://woo.local"
    )
    
    # Mock _get_cart
    mock_cart = {
        "items": [],
        "totals": {
            "total_items": 0,
            "total_price": 0,
            "total_shipping": 0,
            "total_discount": 0,
            "total_tax": 0
        },
        "billing_address": {"first_name": "John"},
        "shipping_address": {"first_name": "John"},
        "shipping_rates": [
            {
                "package_id": 0,
                "shipping_rates": [
                    {
                        "rate_id": "free_shipping:1",
                        "name": "Free shipping",
                        "selected": False
                    }
                ]
            }
        ]
    }
    mock_cart_token = "cart-token-123"
    mock_nonce = "nonce-123"
    
    service._get_cart = AsyncMock(return_value=(mock_cart, mock_cart_token, mock_nonce))
    from unittest.mock import Mock
    service._log_if_error = Mock() # Mock to avoid errors
    
    # Mock responses
    mock_shipping_response = AsyncMock()
    mock_shipping_response.status_code = 200
    mock_shipping_response.json = lambda: mock_cart

    mock_checkout_response = AsyncMock()
    mock_checkout_response.status_code = 200
    mock_checkout_response.json = lambda: {
        "order_id": 123,
        "payment_result": {
            "payment_status": "success",
            "redirect_url": "http://woo.local/order-received/123"
        }
    }
    
    payment_req = PaymentCreateRequest(instruments=[{"id": "instr_1", "type": "card", "handler_id": "stripe"}])
    risk_signals = {}
    idempotency_key = "idem-123"
    
    with patch('httpx.AsyncClient.post', new_callable=AsyncMock) as mock_post:
        def side_effect(url, **kwargs):
            if "select-shipping-rate" in url:
                return mock_shipping_response
            elif "checkout" in url:
                return mock_checkout_response
            raise ValueError(f"Unexpected URL: {url}")
        
        mock_post.side_effect = side_effect
        
        checkout = await service.complete_checkout(
            checkout_id="cart-token-123",
            payment=payment_req,
            risk_signals=risk_signals,
            idempotency_key=idempotency_key
        )
        
        assert checkout.status == CheckoutStatus.COMPLETED
        assert checkout.order.id == "123"
        assert str(checkout.order.permalink_url) == "http://woo.local/order-received/123"
        
        # Verify post called twice
        assert mock_post.call_count == 2
        
        # Verify calls
        calls = mock_post.call_args_list
        
        # First call to select-shipping-rate
        args0, kwargs0 = calls[0]
        assert "select-shipping-rate" in args0[0]
        assert kwargs0['params']['rate_id'] == "free_shipping:1"
        
        # Second call to checkout
        args1, kwargs1 = calls[1]
        assert "checkout" in args1[0]
        assert kwargs1['json']['payment_method'] == "stripe"
