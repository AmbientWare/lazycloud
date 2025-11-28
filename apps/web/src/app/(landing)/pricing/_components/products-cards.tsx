import { ProductCard } from "./product-card";
import { getProducts } from "@/actions/products";

export async function ProductsCards() {
  const products = await getProducts();
  
  const filteredProducts = products.filter(
    (product) => product.name.toLowerCase() !== "enterprise"
  );
  
  if (filteredProducts.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <p className="text-muted-foreground text-lg">
          Unable to load subscription plans. Please try again later.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto grid max-w-6xl grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-3">
      {filteredProducts.map((product) => (
        <ProductCard key={product.id} product={product} />
      ))}
    </div>
  );
}

