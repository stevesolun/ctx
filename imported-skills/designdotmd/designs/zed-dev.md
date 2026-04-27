---
version: alpha
name: Zed Dev
description: Editor-dark, warmer than pitch, glyph-sharp.
colors:
  primary: "#E5E4DF"
  secondary: "#7B7A74"
  tertiary: "#5FB5D6"
  neutral: "#161614"
  surface: "#1D1D1B"
  on-primary: "#161614"
typography:
  display:
    fontFamily: JetBrains Mono
    fontSize: 3.2rem
    fontWeight: 500
    letterSpacing: "-0.02em"
  h1:
    fontFamily: JetBrains Mono
    fontSize: 1.75rem
    fontWeight: 500
  body:
    fontFamily: Inter
    fontSize: 0.92rem
    lineHeight: 1.55
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.72rem
rounded:
  sm: 3px
  md: 6px
  lg: 10px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

A developer-editor palette: warm near-black, cyan accent, mono everywhere.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E5E4DF`):** Headlines and core text.
- **Secondary (`#7B7A74`):** Borders, captions, and metadata.
- **Tertiary (`#5FB5D6`):** The sole driver for interaction. Reserve it.
- **Neutral (`#161614`):** The page foundation.

## Typography

- **display:** JetBrains Mono 3.2rem
- **h1:** JetBrains Mono 1.75rem
- **body:** Inter 0.92rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
