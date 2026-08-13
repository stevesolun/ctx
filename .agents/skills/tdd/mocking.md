# When to Mock

Prefer fakes, stubs, or mocks at boundaries where the real dependency is slow,
nondeterministic, unavailable, or unsafe:

- External APIs (payment, email, etc.)
- Databases when a representative test database is impractical
- Time/randomness
- File systems and other environmental services

Be cautious about mocking:

- Your own classes/modules
- Internal collaborators
- Anything you control

Internal mocks can still be useful for fault injection or narrowly scoped
interaction contracts. Prefer the least synthetic test double that preserves a
fast, trustworthy feedback loop.

## Designing useful seams

At consequential boundaries, design interfaces that are easy to substitute:

**1. Use dependency injection**

Pass external dependencies in rather than creating them internally:

```typescript
// Easy to mock
function processPayment(order, paymentClient) {
  return paymentClient.charge(order.total);
}

// Hard to mock
function processPayment(order) {
  const client = new StripeClient(process.env.STRIPE_KEY);
  return client.charge(order.total);
}
```

**2. Prefer SDK-style interfaces over generic fetchers**

Create specific functions for each external operation instead of one generic function with conditional logic:

```typescript
// GOOD: Each function is independently mockable
const api = {
  getUser: (id) => fetch(`/users/${id}`),
  getOrders: (userId) => fetch(`/users/${userId}/orders`),
  createOrder: (data) => fetch('/orders', { method: 'POST', body: data }),
};

// BAD: Mocking requires conditional logic inside the mock
const api = {
  fetch: (endpoint, options) => fetch(endpoint, options),
};
```

The SDK approach means:
- Each mock returns one specific shape
- No conditional logic in test setup
- Easier to see which endpoints a test exercises
- Type safety per endpoint
