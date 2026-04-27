---
version: alpha
name: Spa Onsen
description: Onsen: wet-stone grey, steam-rose, cedar accent.
colors:
  primary: "#2C2828"
  secondary: "#938B85"
  tertiary: "#D8A87F"
  neutral: "#ECE4DE"
  surface: "#F5EEE7"
  on-primary: "#F5EEE7"
typography:
  display:
    fontFamily: Shippori Mincho
    fontSize: 4rem
    fontWeight: 400
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Shippori Mincho
    fontSize: 2.2rem
    fontWeight: 400
  body:
    fontFamily: Noto Sans JP
    fontSize: 0.98rem
    lineHeight: 1.7
  label:
    fontFamily: Noto Sans JP
    fontSize: 0.72rem
    fontWeight: 400
    letterSpacing: "0.18em"
rounded:
  sm: 2px
  md: 4px
  lg: 8px
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

A Japanese-onsen spa palette: wet-stone surface, steam-rose primary, cedar-wood accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#2C2828`):** Headlines and core text.
- **Secondary (`#938B85`):** Borders, captions, and metadata.
- **Tertiary (`#D8A87F`):** The sole driver for interaction. Reserve it.
- **Neutral (`#ECE4DE`):** The page foundation.

## Typography

- **display:** Shippori Mincho 4rem
- **h1:** Shippori Mincho 2.2rem
- **body:** Noto Sans JP 0.98rem
- **label:** Noto Sans JP 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
