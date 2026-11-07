// Attach to Mutation.publishActionResult with a NONE data source; IAM-only field.
export function request(ctx) {
  return { payload: ctx.args.event };
}
export function response(ctx) {
  return ctx.result;
}
