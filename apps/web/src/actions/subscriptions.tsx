"use server";

import { cacheLife } from 'next/cache'
import polarService from "@/server/polar";

export async function getCustomerState( userId: string) {
  "use cache";
  cacheLife('minutes')
  try {
    const customerState = await polarService.getCustomerStateExternal(userId);
    return customerState;
  } catch {
    return null;
  }
}
